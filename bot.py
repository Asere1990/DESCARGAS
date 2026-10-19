import os
from aiohttp import web
from descargas_app import create_app

if __name__ == '__main__':
    os.umask(0o077)
    web.run_app(create_app(),host='0.0.0.0',port=int(os.environ.get('PORT','10000')),access_log=None)
