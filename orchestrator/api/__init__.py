from .user import api_namespace_user
from .zenstream import api_namespace_zs

from .user import (
    authenticate,
    check_invite,
    delete_invite,
    generate_invite,
    login,
    logout,
    me,
    register,
)
from .zenstream import locale, subtitles, syncplay, version
from . import admin

api_namespaces = [
    api_namespace_user,
    api_namespace_zs,
]
