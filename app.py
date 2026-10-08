from flask import Flask

from utils.scheduling_routes import scheduling_api


def create_app():
    app = Flask(__name__)
    app.register_blueprint(scheduling_api)
    return app


app = create_app()


if __name__ == "__main__":
    app.run()
