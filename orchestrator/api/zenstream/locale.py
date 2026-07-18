from flask import request
from flask_restx import Resource

from app.models.preference import SUPPORTED_LOCALES, UserPreference
from jellyfin.api_service import authenticated_user_id
from . import api_namespace_zs


@api_namespace_zs.route("preferences/locale")
class LocalePreference(Resource):
    def get(self):
        user_id, error = _authenticated_user()
        if error:
            return error
        return {"locale": UserPreference(user_id).get_locale()}, 200

    def patch(self):
        user_id, error = _authenticated_user()
        if error:
            return error
        payload = request.get_json(silent=True) or {}
        locale = payload.get("locale")
        if locale not in SUPPORTED_LOCALES:
            return {"message": "Locale must be one of: en, ja."}, 400
        return {"locale": UserPreference(user_id).set_locale(locale)}, 200


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
