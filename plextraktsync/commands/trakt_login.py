from __future__ import annotations

from json import JSONDecodeError
from os.path import exists
from typing import TYPE_CHECKING

from rich.prompt import Prompt
from trakt.errors import ForbiddenException

from plextraktsync.factory import factory
from plextraktsync.path import config_dir, pytrakt_file
from plextraktsync.style import error, prompt, success, title

if TYPE_CHECKING:
    from plextraktsync.trakt.TraktApi import TraktApi

PROMPT_TRAKT_CLIENT_ID = prompt("Please enter your client id")
PROMPT_TRAKT_CLIENT_SECRET = prompt("Please enter your client secret")
TRAKT_LOGIN_SUCCESS = success("You are now logged into Trakt. Your Trakt credentials have been added in .env and .pytrakt.json files.")


def trakt_authenticate(api: TraktApi):
    print = factory.print

    print(title("Sign in to Trakt"))

    print("If you do not have a Trakt client ID and secret:")
    print("      1 - Open https://trakt.tv/oauth/applications on any computer")
    print("      2 - Login to your Trakt account")
    print("      3 - Press the NEW APPLICATION button")
    print("      4 - Set the NAME field = plex")
    print("      5 - Set the REDIRECT URL field = urn:ietf:wg:oauth:2.0:oob")
    print("      6 - Press the SAVE APP button")
    print("")

    while True:
        client_id = Prompt.ask(PROMPT_TRAKT_CLIENT_ID)
        client_secret = Prompt.ask(PROMPT_TRAKT_CLIENT_SECRET, password=True)

        print("Attempting to authenticate with Trakt")
        try:
            return api.device_auth(client_id=client_id, client_secret=client_secret)
        except (ForbiddenException, JSONDecodeError) as e:
            print(error(f"Log in to Trakt failed: {e}, Try again."))


def has_trakt_token():
    from pathlib import Path

    token_file = factory.config["TRAKT_BROWSER_TOKEN_FILE"]
    if token_file or (Path(config_dir) / "trakt-auth.json").exists():
        from plextraktsync.trakt.PkceAuth import TraktAuth

        TraktAuth(token_file or Path(config_dir) / "browser-token.json", config_dir).read()
        return factory.config["TRAKT_USERNAME"] is not None

    if not exists(pytrakt_file):
        return False

    CONFIG = factory.config
    return CONFIG["TRAKT_USERNAME"] is not None


def trakt_login_autoconfig():
    login()


def trakt_login(method=None, redirect_uri=None, allow_local_http=False):
    """
    Log in to Trakt Account to obtain Access Token.
    """
    if method:
        from pathlib import Path
        from urllib.parse import parse_qs, urlsplit

        from plextraktsync.trakt.PkceAuth import TraktAuth

        auth = TraktAuth(factory.config["TRAKT_BROWSER_TOKEN_FILE"] or Path(config_dir) / "browser-token.json", config_dir)
        if method == "pkce":
            if not redirect_uri:
                from click import ClickException

                raise ClickException("Provide --redirect-uri with the callback registered for your Trakt app.")
            factory.print(auth.start(redirect_uri, allow_local_http=allow_local_http))
            callback = Prompt.ask("Open that URL, approve your Trakt app, then paste the complete callback URL")
            actual, expected = urlsplit(callback), urlsplit(redirect_uri)
            if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path):
                from click import ClickException

                raise ClickException("Callback URL does not match the registered redirect.")
            params = parse_qs(actual.query)
            auth.finish(params.get("code", [""])[0], params.get("state", [""])[0])
        else:
            auth.select(method)
        login()
        factory.print("Trakt authentication method saved. Future commands use this method.")
        return
    login()


def login():
    print = factory.print
    api = factory.trakt_api
    if not api.external_auth:
        trakt_authenticate(api)
    user = api.me.username

    CONFIG = factory.config
    CONFIG["TRAKT_USERNAME"] = user
    CONFIG.save()

    if api.external_auth:
        print(success("Trakt credentials verified; username saved. Credentials remain in the external token file."))
    else:
        print(TRAKT_LOGIN_SUCCESS)
