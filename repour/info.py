import asyncio
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

#
# Info/Documentation endpoint
#

def get_info_endpoint_handler():

    info_data = None

    @asyncio.coroutine
    def handle(request):
        nonlocal info_data
        if info_data is None:
            info_data = ""
            with open("README.html", "r") as file:
                info_data += file.read()
            info_data = info_data.encode('utf-8')
        return web.Response(body = info_data)

    return handle
