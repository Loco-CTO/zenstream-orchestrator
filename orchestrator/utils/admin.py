from functools import wraps

from app.models.admin import Admin
from flask import request


def authenticate_admin(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        username = request.headers.get("Username")
        token = request.headers.get("TOKEN")
        if not isinstance(username, str) or not isinstance(token, str):
            return {"message": "Administrator authentication required."}, 401
        if not Admin(username).authenticate(token):
            return {"message": "Administrator authentication failed."}, 403
        return func(*args, **kwargs)

    return wrapper
