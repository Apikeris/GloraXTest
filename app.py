"""WSGI entry point. No writes, scraping or debug mode on import."""

from glorax import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=False)
