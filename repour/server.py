import asyncio
import base64
import collections
import hashlib
import json
import logging
import os
import traceback

import aiohttp
from aiohttp import web
import voluptuous

from . import adjust
from . import exception
from . import pull
from . import repo
from . import validation

logger = logging.getLogger(__name__)

def create_callback_id():
    return base64.b32encode(os.urandom(30)).decode("ascii")

def create_traceback_id():
    tb = traceback.format_exc()
    h = hashlib.md5()
    h.update(tb.encode("utf-8"))
    return h.hexdigest()

def described_error_to_obj(exception):
    traceback_id = create_traceback_id()
    error = {k: v for k, v in exception.__dict__.items() if not k.startswith("_")}
    error["error_type"] = exception.__class__.__name__
    error["error_traceback"] = traceback_id
    return (traceback_id, error)

def exception_to_obj(exception):
    traceback_id = create_traceback_id()
    error = {
        "error_type": exception.__class__.__name__,
        "error_traceback": traceback_id,
    }
    return (traceback_id, error)

def _validated_json_endpoint(validator, coro):
    client_session = aiohttp.ClientSession() #pylint: disable=no-member

    @asyncio.coroutine
    def handler(request):
        spec = yield from request.json()
        try:
            validator(spec)
        except voluptuous.MultipleInvalid as x:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps(
                    obj=[e.__dict__ for e in x.errors],
                    ensure_ascii=False,
                ),
            )

        try:
            # Callback requested?
            validation.callback(spec)
        except voluptuous.MultipleInvalid as x:
            try:
                ret = yield from coro(spec, **request.app)
            except exception.DescribedError as e:
                status = 400
                traceback_id, obj = described_error_to_obj(e)
                logger.exception(traceback_id)
            except Exception as e:
                status = 500
                traceback_id, obj = exception_to_obj(e)
                logger.exception(traceback_id)
            else:
                status = 200
                obj = ret

        else:
            callback_id = create_callback_id()

            @asyncio.coroutine
            def send_callback(callback_spec):
                try:
                    ret = yield from coro(spec, **request.app)
                except exception.DescribedError as e:
                    status = 400
                    traceback_id, obj = described_error_to_obj(e)
                    logger.exception(traceback_id)
                except Exception as e:
                    status = 500
                    traceback_id, obj = exception_to_obj(e)
                    logger.exception(traceback_id)
                else:
                    status = 200
                    obj = ret

                obj["callback"] = {
                    "status": status,
                    "id": callback_id,
                }

                client_session.request(
                    callback_spec["method"],
                    callback_spec["url"],
                    data=json.dumps(
                        obj=obj,
                        ensure_ascii=False,
                    ).encode("utf-8")
                )

            request.app.loop.create_task(send_callback(spec["callback"]))
            status = 202
            obj = {
                "callback": {
                    "id": callback_id,
                }
            }

        response = web.Response(
            status=status,
            content_type="application/json",
            text=json.dumps(
                obj=obj,
                ensure_ascii=False,
            ),
        )
        return response
    return handler

#
# Handlers
#

@asyncio.coroutine
def show_id(request):
    return web.Response(
        content_type="text/plain",
        text="Repour",
    )

pull_source = _validated_json_endpoint(validation.pull, pull.pull)

adjust_source = _validated_json_endpoint(validation.adjust, adjust.adjust)

#
# Setup
#

@asyncio.coroutine
def init(loop, bind, repo_provider, adjust_provider):
    logger.debug("Running init")
    app = web.Application(loop=loop)

    logger.debug("Adding application resources")
    app["repo_provider"] = repo.provider_types[repo_provider["type"]](**repo_provider["params"])
    app["adjust_provider"] = adjust.provider_types[adjust_provider["type"]](**adjust_provider["params"])

    logger.debug("Setting up handlers")
    app.router.add_route("POST", "/pull", pull_source)
    app.router.add_route("POST", "/adjust", adjust_source)

    logger.debug("Creating asyncio server")
    srv = yield from loop.create_server(app.make_handler(), bind["address"], bind["port"])
    for socket in srv.sockets:
        logger.info("Server started on socket: {}".format(socket.getsockname()))

def start_server(bind, repo_provider, adjust_provider):
    logger.debug("Starting server")
    loop = asyncio.get_event_loop()

    # Monkey patch for Python 3.4.1
    if not hasattr(loop, "create_task"):
        loop.create_task = lambda c: asyncio.async(c, loop=loop)

    loop.run_until_complete(init(
        loop=loop,
        bind=bind,
        repo_provider=repo_provider,
        adjust_provider=adjust_provider,
    ))

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.debug("KeyboardInterrupt")
    finally:
        logger.info("Stopping tasks")
        tasks = asyncio.Task.all_tasks()
        for task in tasks:
            task.cancel()
        results = loop.run_until_complete(asyncio.gather(*tasks, loop=loop, return_exceptions=True))
        exception_results = [r for r in results if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError)]
        if len(exception_results) > 1:
            raise Exception(exception_results)
        elif len(exception_results) == 1:
            raise exception_results[0]
        loop.close()
