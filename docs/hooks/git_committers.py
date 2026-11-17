"""MkDocs hooks for the git-committers plugin (see mkdocs.yml).

Without a GitHub token the plugin warns, which `mkdocs build --strict` turns
into a failed build, and asks the API anonymously: a repository that is not
public answers each page with a 404 logged as an error, on every rebuild, and
a public one allows 60 anonymous calls an hour. A build without a token runs
without the contributors section instead. CI and the Pages deploy always pass
a token.

The theme writes each contributor's `name` into an HTML attribute without
escaping it (MkDocs templates do not autoescape), and for a co-author the
plugin sets it to the GitHub display name: free text, which its owner can
change at any time. Pages show the login instead, as they already do for
commit authors; a login holds no quote or angle bracket.
"""

import logging
import os

from mkdocs.plugins import event_priority

log = logging.getLogger("mkdocs.hooks.git_committers")

PLUGIN_NAME = "git-committers"
TOKEN_VARIABLE = "MKDOCS_GIT_COMMITTERS_APIKEY"


@event_priority(100)  # before the plugin's own on_config reads `enabled`
def on_config(config):
    plugin = config.plugins.get(PLUGIN_NAME)
    if plugin is None or not plugin.config["enabled"]:
        return config
    if plugin.config["token"] or os.environ.get(TOKEN_VARIABLE):
        return config
    plugin.config["enabled"] = False
    log.info("No GitHub token in %s: building without the page contributors.", TOKEN_VARIABLE)
    return config


@event_priority(-100)  # after the plugin filled context["committers"]
def on_page_context(context, page, config, nav):
    committers = context.get("committers")
    if committers:
        # New dicts: the plugin keeps these very objects in its cache file.
        context["committers"] = [{**author, "name": author.get("login", "")} for author in committers]
    return context
