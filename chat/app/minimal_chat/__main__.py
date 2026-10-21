import uvicorn
from .server import Config, create_app

if __name__ == '__main__':
    uvicorn.run(create_app(Config.existing()), host='127.0.0.1', port=8873, access_log=False)
