from flask import Flask, Response
from threading import Thread

app = Flask(__name__)

HTML = """
      <!DOCTYPE html>
      <html lang="en">
      <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Status</title>
        <style>
          * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
          }
          
          body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
            background: #f5f5f7;
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
          }
          
          .container {
            text-align: center;
          }
          
          .status-indicator {
            width: 12px;
            height: 12px;
            background: #34c759;
            border-radius: 50%;
            display: inline-block;
            margin-right: 8px;
            animation: pulse 2s infinite;
          }
          
          @keyframes pulse {
            0%, 100% {
              opacity: 1;
            }
            50% {
              opacity: 0.5;
            }
          }
          
          h1 {
            font-size: 32px;
            font-weight: 500;
            color: #1d1d1f;
            letter-spacing: -0.5px;
          }
          
          p {
            font-size: 16px;
            color: #86868b;
            margin-top: 12px;
            font-weight: 400;
          }
        </style>
      </head>
      <body>
        <div class="container">
          <h1><span class="status-indicator"></span>System Active</h1>
          <p>All systems operational</p>
        </div>
      </body>
      </html>
    """


@app.route("/")
def home():
    return Response(HTML, mimetype="text/html")


def run():
    app.run(host="0.0.0.0", port=10000)


def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
