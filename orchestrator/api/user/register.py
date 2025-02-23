from . import api_namespace_user
from flask_restx import Resource, reqparse
from app.config import Config
from hashlib import sha256
from flask import request
from urllib.parse import urlparse

@api_namespace_user.route("user/register")
class UserRegister(Resource):
    """
    Resource class for user registration.
    """

    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Username", type=str, help="The username.", location="headers")
    get_parser.add_argument("Password", type=str, help="The password.", location="headers")
    get_parser.add_argument("url", type=str, help="The url.", location="headers")

    @api_namespace_user.doc(parser=get_parser)
    @api_namespace_user.response(201, "Registered the user.")
    @api_namespace_user.response(403, "Invite invalid")
    @api_namespace_user.response(409, "Failed to register the user.")
    @api_namespace_user.response(500, "An error occurred while registering.")
    def post(self):
        """
        Register the user.
        """
        args = self.get_parser.parse_args()
        username = args.get("Username").strip()
        password = sha256(args.get("Password").strip().encode()).hexdigest()
        invite_id = urlparse(request.headers.get("Referer")).path.split("/")[-2]

        db = Config()._database

        check = db.execute("SELECT * FROM invites WHERE url = ?", (invite_id,))
        if not check:
            return {}, 403
        
        try:
            db.execute("INSERT INTO users VALUES (?, ?, '{}')", (username, password))
            return {}, 201
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return {}, 409
            else:
                return {}, 500
