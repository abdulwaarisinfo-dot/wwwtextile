"""Users, master data, notifications, own-password page."""
from __future__ import annotations

from starlette.routing import Route

from .errors import ValidationFailed
from .permissions import ROLE_LABELS, ROLES
from .services import master, notifications, users
from .webcore import SESSION_COOKIE, handler, _int


# ---------------------------------------------------------------- users
def _users_page(ctx, errors=None, values=None, status=200):
    return ctx.render("admin_users.html", status, rows=users.list_users(ctx.conn), roles=sorted(ROLES), role_labels=ROLE_LABELS,
                      errors=errors or {}, values=values or {}, min_pw=users.MIN_PASSWORD)


@handler(perm="user.manage")
def users_page(ctx):
    return _users_page(ctx)


@handler(perm="user.manage", post=True)
def user_create(ctx):
    try:
        users.create_user(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        return _users_page(ctx, e.errors, {k: v for k, v in ctx.form.items() if k != "password"}, 422)
    ctx.flash("ok", "User created. They must change the password at first sign-in.")
    return ctx.redirect("/admin/users")


def _user_action(fn):
    def run(ctx, uid):
        try:
            msg = fn(ctx, int(uid))
        except ValidationFailed as e:
            ctx.flash("error", "; ".join(e.errors.values()))
            return ctx.redirect("/admin/users")
        ctx.flash("ok", msg)
        return ctx.redirect("/admin/users")
    run.__name__ = fn.__name__
    return handler(perm="user.manage", post=True)(run)


@_user_action
def user_role(ctx, uid):
    users.set_role(ctx.conn, ctx.user, uid, ctx.form.get("role", ""))
    return "Role changed. Existing sessions of that user are ended."


@_user_action
def user_active(ctx, uid):
    on = ctx.form.get("active") == "1"
    users.set_active(ctx.conn, ctx.user, uid, on)
    return "User activated." if on else "User deactivated and signed out."


@_user_action
def user_reset(ctx, uid):
    users.reset_password(ctx.conn, ctx.user, uid, ctx.form.get("password", ""))
    return "Password reset. The user must change it at next sign-in."


@_user_action
def user_unlock(ctx, uid):
    users.unlock(ctx.conn, ctx.user, uid)
    return "Account unlocked."


# ---------------------------------------------------------------- master data
def _master_page(ctx, perrors=None, pvalues=None, ierrors=None, ivalues=None, status=200):
    return ctx.render("admin_master.html", status, parties=master.list_parties(ctx.conn), items=master.list_items(ctx.conn), kinds=master.KINDS,
                      perrors=perrors or {}, pvalues=pvalues or {}, ierrors=ierrors or {}, ivalues=ivalues or {})


@handler(perm="master.manage")
def master_page(ctx):
    return _master_page(ctx)


@handler(perm="master.manage", post=True)
def party_create(ctx):
    try:
        master.create_party(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        return _master_page(ctx, perrors=e.errors, pvalues=ctx.form, status=422)
    ctx.flash("ok", "Party added.")
    return ctx.redirect("/admin/master")


@handler(perm="master.manage", post=True)
def item_create(ctx):
    try:
        master.create_item(ctx.conn, ctx.user, ctx.form)
    except ValidationFailed as e:
        return _master_page(ctx, ierrors=e.errors, ivalues=ctx.form, status=422)
    ctx.flash("ok", "Fabric item added.")
    return ctx.redirect("/admin/master")


# ---------------------------------------------------------------- own password
def _pw_page(ctx, errors=None, status=200):
    return ctx.render("account_password.html", status, errors=errors or {}, forced=bool(ctx.user["must_change_password"]), min_pw=users.MIN_PASSWORD)


@handler()
def password_page(ctx):
    return _pw_page(ctx)


@handler(post=True)
def password_submit(ctx):
    try:
        users.change_password(ctx.conn, ctx.user, ctx.form.get("current", ""), ctx.form.get("new", ""), ctx.form.get("confirm", ""),
                              ctx.request.cookies.get(SESSION_COOKIE))
    except ValidationFailed as e:
        return _pw_page(ctx, e.errors, 422)
    ctx.flash("ok", "Password changed. Other sessions were signed out.")
    return ctx.redirect("/")


# ---------------------------------------------------------------- notifications
@handler()
def notifications_page(ctx):
    return ctx.render("notifications.html", rows=notifications.list_for(ctx.conn, ctx.user, 100, ctx.request.query_params.get("unread") == "1"),
                      unread=notifications.unread_count(ctx.conn, ctx.user), only_unread=ctx.request.query_params.get("unread") == "1")


@handler(post=True)
def notification_read(ctx, nid):
    notifications.mark_read(ctx.conn, ctx.user, int(nid))
    return ctx.redirect(ctx.back("/notifications"))


@handler(post=True)
def notifications_read_all(ctx):
    notifications.mark_all_read(ctx.conn, ctx.user)
    ctx.flash("ok", "All notifications marked as read.")
    return ctx.redirect("/notifications")


G, P = ["GET"], ["POST"]
ROUTES = [
    Route("/admin/users", users_page, methods=G), Route("/admin/users", user_create, methods=P),
    Route("/admin/users/{uid:int}/role", user_role, methods=P), Route("/admin/users/{uid:int}/active", user_active, methods=P),
    Route("/admin/users/{uid:int}/reset", user_reset, methods=P), Route("/admin/users/{uid:int}/unlock", user_unlock, methods=P),
    Route("/admin/master", master_page, methods=G), Route("/admin/parties", party_create, methods=P), Route("/admin/items", item_create, methods=P),
    Route("/account/password", password_page, methods=G), Route("/account/password", password_submit, methods=P),
    Route("/notifications", notifications_page, methods=G), Route("/notifications/read-all", notifications_read_all, methods=P),
    Route("/notifications/{nid:int}/read", notification_read, methods=P),
]
