from functools import wraps

from app.models.user import User
from flask import request


def authenticate(func):
    """
    Authenticate decorator to ensure that the user is authenticated before
    executing the decorated function.

    Args:
        func (function): The function to be decorated.

    Returns:
        function: The wrapped function with authentication check.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        """
        Wrapper function to check user authentication.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            function: The original function if the user is authenticated.
            dict: A message indicating the user is not authenticated with a 401 status code.
        """
        token = request.headers.get("TOKEN")
        user = request.headers.get("Username")
        if type(user) is not str or type(token) is not str:
            return {}, 403

        if User(user).authenticate(token):
            return {}, 202
        return {}, 403

    return wrapper
