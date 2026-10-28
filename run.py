import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("FLASK_PORT", 5001))
    debug = os.getenv("FLASK_DEBUG", "true").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)

