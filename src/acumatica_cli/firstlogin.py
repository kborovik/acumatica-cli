"""First-login admin password initialization via the Login.aspx screen flow.

A freshly created tenant seeds ``admin``/``setup`` with a
must-change-on-first-login flag the contract REST API cannot clear (verified —
see docs/rest-api.md). The sign-in screen's WebForms flow can, with plain
HTTP: GET the page for the hidden fields, POST the seed credentials, then
POST again with the new password into the change view the server renders.
"""

import time
from contextlib import suppress
from html.parser import HTMLParser

import httpx

from .config import Instance

LOGIN_PATH = "/Frames/Login.aspx"
SEED_PASSWORD = "setup"  # what a new tenant's admin is born with

TENANT_FIELD = "ctl00$phUser$cmbCompany"
USER_FIELD = "ctl00$phUser$txtUser"
PASS_FIELD = "ctl00$phUser$txtPass"
NEW_PASS_FIELD = "ctl00$phUser$txtNewPassword"
CONFIRM_PASS_FIELD = "ctl00$phUser$txtConfirmPassword"
# The page has four submit inputs; this is the right one (mfLoginButton is the
# multi-factor flow). Both new-password fields must be posted together.
LOGIN_BUTTON = "ctl00$phUser$btnLogin"
REMEMBER_FIELD = "ctl00$phUser$rememberDevice"


class _LoginForm(HTMLParser):
    """Collects the sign-in form's postable fields (inputs and selects)."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self._select: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        name = a.get("name")
        if tag == "input" and name:
            itype = (a.get("type") or "text").lower()
            if itype in ("submit", "button"):
                return  # buttons are posted explicitly by the caller
            if itype == "checkbox" and "checked" not in a:
                return
            self.fields[name] = a.get("value") or ""
        elif tag == "select" and name:
            self._select = name
            self.fields[name] = ""
        elif tag == "option" and self._select and "selected" in a:
            self.fields[self._select] = a.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None


def _form_fields(html: str) -> dict[str, str]:
    parser = _LoginForm()
    parser.feed(html)
    return parser.fields


def _rest_login_works(instance: Instance, tenant: str) -> bool:
    """One REST login attempt with the instance credentials (then logout)."""
    with httpx.Client(base_url=instance.base_url, timeout=60) as http:
        r = http.post(
            "/entity/auth/login",
            json={
                "name": instance.username,
                "password": instance.password,
                "tenant": tenant,
            },
        )
        if r.status_code == 204:
            http.post("/entity/auth/logout", content=b"")
            return True
    return False


def _get_login_page(http: httpx.Client, retries: int, delay: float) -> httpx.Response:
    """GET the sign-in page, retrying while the app warms up after a recycle."""
    last = ""
    for attempt in range(retries):
        try:
            r = http.get(LOGIN_PATH)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except httpx.TransportError as e:
            last = repr(e)
        if attempt < retries - 1:
            time.sleep(delay)
    raise RuntimeError(f"sign-in page did not come back after recycle: {last}")


def initialize_admin_password(
    instance: Instance, tenant: str, retries: int = 24, delay: float = 5.0
) -> str:
    """Change the tenant's seeded admin password to the instance password.

    Returns ``"already initialized"`` if REST login already works (the normal
    case — ``acu tenant create`` presets the password via ac.exe), or
    ``"password changed"`` after a successful screen-flow change (verified by
    a REST login). Raises RuntimeError when neither path succeeds.
    """
    with httpx.Client(
        base_url=instance.base_url,
        timeout=httpx.Timeout(10.0, read=300.0),
        follow_redirects=True,
    ) as http:
        # fetch the page first: it doubles as the wait for the app to warm up
        # after the post-create app-pool recycle
        page = _get_login_page(http, retries, delay)
        if _rest_login_works(instance, tenant):
            return "already initialized"

        fields = _form_fields(page.text)
        fields.pop(REMEMBER_FIELD, None)
        fields.update(
            {
                TENANT_FIELD: tenant,
                USER_FIELD: instance.username,
                PASS_FIELD: SEED_PASSWORD,
                LOGIN_BUTTON: "Sign In",
            }
        )
        page = http.post(LOGIN_PATH, data=fields)

        fields = _form_fields(page.text)
        if NEW_PASS_FIELD not in fields:
            state = (
                "seed password signed straight in — no change demanded; "
                "the admin password is still the seed"
                if "Main" in page.url.path
                else f"landed on {page.url}"
            )
            raise RuntimeError(
                f"tenant {tenant!r}: expected the password-change view after "
                f"signing in with the seed password; {state}"
            )
        fields.pop(REMEMBER_FIELD, None)
        fields.update(
            {
                TENANT_FIELD: tenant,
                USER_FIELD: instance.username,
                PASS_FIELD: SEED_PASSWORD,
                NEW_PASS_FIELD: instance.password,
                CONFIRM_PASS_FIELD: instance.password,
                LOGIN_BUTTON: "Sign In",
            }
        )
        page = http.post(LOGIN_PATH, data=fields)
        with suppress(httpx.HTTPError):
            # the screen session holds a license seat — best-effort logout
            http.post("/entity/auth/logout", content=b"")
        if "Main" not in page.url.path:
            raise RuntimeError(
                f"tenant {tenant!r}: password change not accepted "
                f"(landed on {page.url})"
            )

    if not _rest_login_works(instance, tenant):
        raise RuntimeError(
            f"tenant {tenant!r}: password change looked accepted but REST "
            "login with the new password failed"
        )
    return "password changed"
