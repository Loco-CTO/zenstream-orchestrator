from flask import request
from flask_restx import Resource, fields, reqparse
from app.models.admin import Admin
from app.models.user import User
from utils.admin import authenticate_admin
from . import api_namespace_user


login_parser = reqparse.RequestParser()
login_parser.add_argument("Username", type=str, required=True, location="headers")
login_parser.add_argument("Password", type=str, required=True, location="headers")


@api_namespace_user.route("admin/login")
class AdminLogin(Resource):
    @api_namespace_user.doc(parser=login_parser)
    def post(self):
        args = login_parser.parse_args()
        token = Admin(args["Username"].strip()).login(args["Password"])
        if not token:
            return {"message": "Invalid administrator credentials."}, 403
        return {}, 202, {"TOKEN": token}


admin_parser = reqparse.RequestParser()
admin_parser.add_argument("Username", type=str, required=True, location="headers")
admin_parser.add_argument("TOKEN", type=str, required=True, location="headers")
admin_parser.add_argument("Target-Username", type=str, location="headers")
admin_parser.add_argument("New-Password", type=str, location="headers")
admin_parser.add_argument("New-Username", type=str, location="headers")


@api_namespace_user.route("admin/logout")
class AdminLogout(Resource):
    @api_namespace_user.doc(parser=admin_parser)
    @authenticate_admin
    def post(self):
        args = admin_parser.parse_args()
        Admin(args["Username"]).logout(args["TOKEN"])
        return {}, 204


@api_namespace_user.route("admin/profile")
class AdminProfile(Resource):
    @api_namespace_user.doc(parser=admin_parser)
    @authenticate_admin
    def get(self):
        args = admin_parser.parse_args()
        return Admin(args["Username"]).profile() or {"message": "Account not found."}, 200

    @api_namespace_user.doc(parser=admin_parser)
    @authenticate_admin
    def patch(self):
        args = admin_parser.parse_args()
        try:
            result = Admin(args["Username"]).update_profile(args.get("New-Username"), args.get("New-Password"), args["TOKEN"])
            return {"username": result["username"]}, 200
        except ValueError as error:
            return {"message": str(error)}, 400


@api_namespace_user.route("admin/overview")
class AdminOverview(Resource):
    @api_namespace_user.doc(parser=admin_parser)
    @authenticate_admin
    def get(self):
        args = admin_parser.parse_args()
        db = Admin(args["Username"])._db
        users = db.execute("SELECT COUNT(*), SUM(CASE WHEN COALESCE(disabled, 0) = 0 THEN 1 ELSE 0 END), SUM(CASE WHEN COALESCE(disabled, 0) = 1 THEN 1 ELSE 0 END) FROM users")[0]
        return {"users": users[0] or 0, "active_users": users[1] or 0, "disabled_users": users[2] or 0, "administrators": db.execute("SELECT COUNT(*) FROM admins WHERE disabled = 0")[0][0], "pending_invites": db.execute("SELECT COUNT(*) FROM invites")[0][0]}, 200


@api_namespace_user.route("admin/accounts")
class AdminAccounts(Resource):
    model = api_namespace_user.model(
        "AdminAccount", {
            "username": fields.String,
            "is_root": fields.Boolean,
            "disabled": fields.Boolean,
        }
    )

    @api_namespace_user.doc(parser=admin_parser)
    @api_namespace_user.marshal_with(model, as_list=True)
    @authenticate_admin
    def get(self):
        args = admin_parser.parse_args()
        return Admin(args["Username"]).list_accounts(), 200

    @api_namespace_user.doc(parser=admin_parser)
    @authenticate_admin
    def post(self):
        args = admin_parser.parse_args()
        if not args.get("Target-Username") or not args.get("New-Password"):
            return {"message": "Target-Username and New-Password are required."}, 400
        if not args["Target-Username"].strip() or len(args["New-Password"]) < 8:
            return {"message": "Username cannot be empty and password must be at least 8 characters."}, 400
        if not Admin(args["Username"]).create(args["Target-Username"], args["New-Password"]):
            return {"message": "Administrator already exists."}, 409
        return {}, 201


@api_namespace_user.route("admin/accounts/<string:username>")
class AdminAccount(Resource):
    @api_namespace_user.doc(parser=admin_parser)
    @authenticate_admin
    def patch(self, username):
        args = admin_parser.parse_args()
        target = Admin(args["Username"])
        if args.get("New-Password"):
            return ({}, 200) if target.rotate_password(username, args["New-Password"]) else ({"message": "Account not found."}, 404)
        disabled = request.args.get("disabled", "true").lower() == "true"
        return ({}, 200) if target.set_disabled(username, disabled) else ({"message": "Root account cannot be disabled or account was not found."}, 403)


@api_namespace_user.route("admin/users")
class AdminUsers(Resource):
    model = api_namespace_user.model("ManagedUser", {"username": fields.String, "disabled": fields.Boolean})

    @api_namespace_user.doc(parser=admin_parser)
    @api_namespace_user.marshal_with(model, as_list=True)
    @authenticate_admin
    def get(self):
        return User.list_accounts(), 200


@api_namespace_user.route("admin/users/<string:username>")
class AdminUser(Resource):
    @api_namespace_user.doc(parser=admin_parser)
    @authenticate_admin
    def patch(self, username):
        args = admin_parser.parse_args()
        if args.get("New-Password"):
            if len(args["New-Password"]) < 8:
                return {"message": "Password must be at least 8 characters."}, 400
            return ({}, 200) if User.reset_password(username, args["New-Password"]) else ({"message": "User not found."}, 404)
        disabled = request.args.get("disabled", "true").lower() == "true"
        return ({}, 200) if User.set_disabled_account(username, disabled) else ({"message": "User not found."}, 404)

    @api_namespace_user.doc(parser=admin_parser)
    @authenticate_admin
    def delete(self, username):
        return ({}, 204) if User.delete_account(username) else ({"message": "User not found."}, 404)
