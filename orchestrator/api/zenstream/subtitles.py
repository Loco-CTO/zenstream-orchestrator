from flask import request
from flask_restx import Resource

from app.models.preference import UserPreference
from jellyfin.api_service import authenticated_user_id
from . import api_namespace_zs


@api_namespace_zs.route("preferences/subtitles")
class SubtitlePreference(Resource):
    def get(self):
        user_id, error = _authenticated_user()
        if error:
            return error
        return UserPreference(user_id).get_subtitle_style(), 200

    def patch(self):
        user_id, error = _authenticated_user()
        if error:
            return error
        try:
            return UserPreference(user_id).set_subtitle_style(
                request.get_json(silent=True) or {}
            ), 200
        except ValueError as exc:
            return {"message": str(exc)}, 400


def _authenticated_user():
    token = request.headers.get("X-Jellyfin-Token")
    if not token:
        return None, ({"message": "Authentication required."}, 401)
    try:
        user_id = authenticated_user_id(token)
    except RuntimeError as error:
        return None, ({"message": str(error)}, 503)
    if not user_id:
        return None, ({"message": "Invalid token."}, 401)
    return user_id, None
