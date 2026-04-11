import json
import requests

_URL = "https://text.pollinations.ai/openai"
_MODEL = "openai-fast"
BLAND_ERROR = "Something went wrong on my end and the download didn't finish."

_SYSTEM = (
    'Suppose you are "Downloader". A discord bot. You can download videos using just yt-dlp alone. '
    "So you support the corresponding services yt-dlp does. Now, when you get an error, your purpose "
    "is to respond within 2 sentences to summarize the error to the USER, NOT the owner of the bot. "
    "By the way, the errors you will get are on your own side. So your response will be just telling "
    "them by the opposing side. Don't get too technical, ever and use casual normal, human-sounding words. "
    "Stay consistent with how you respond. Always respond how the bot would when it gets an error. "
    "Do not ever tell the user to try anything, they don't have any control. "
    "Your task is only to describe that error. "
    "In your messages, do not EVER use em dashes. That is 100% forbidden.\n\n"
    "Example: Error: payload_too_large\n"
    "your response: The file successfully downloaded but the file was too large to upload through discord.\n\n"
    "Here are the logs: {logs}"
)


def explain_error(logs: str) -> str:
    try:
        payload = json.dumps(
            {
                "model": _MODEL,
                "messages": [{"role": "user", "content": _SYSTEM.format(logs=logs)}],
            }
        )
        response = requests.post(
            _URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=(5, 20),
        )
        if response.status_code < 200 or response.status_code >= 300:
            return BLAND_ERROR
        data = response.json()
        message = data["choices"][0]["message"]["content"].strip()
        if not message:
            return BLAND_ERROR
        return message.replace("—", "-")
    except (KeyError, IndexError, TypeError, ValueError, requests.RequestException):
        return BLAND_ERROR
