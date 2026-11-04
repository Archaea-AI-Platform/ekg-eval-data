"""Access scopes: one AccessScope per place, granted to whoever the tool grants it to.

Every Apache place is public, so each scope is granted to the Public principal at
read level. A private deployment replaces those grants with the tool's real members
(repo collaborators, channel members, Jira roles); the records keep the same scope.
"""

from .common import Graph


def place(g: Graph, tool: str, kind: str, native: str, history: str = "all", public: bool = True):
    """Create the AccessScope for a place and return the scope to put on its records.

    public=True grants it to the Public principal (every Apache place); otherwise the
    adapter grants it to the tool's real members with grant()."""
    public_principal = g.ctx.get("public")
    if public is not False and public_principal is None:
        public_principal = g.ctx["public"] = g.rec("Public", "ekg", "public", {}, scope=[["ekg:public-principal"]])
    s = g.rec("AccessScope", "ekg", f"{tool}:{kind}:{native}", {"kind": kind, "tool": tool, "history": history},
              scope=[["ekg:public-principal"]])
    s["scope"] = [[s["id"]]]          # a scope record is visible to whoever holds it
    if public:
        g.link("CAN_ACCESS", public_principal, s, "snapshot", level="read", via="base_permission")
    g.ctx.setdefault("scope_by_tool", {}).setdefault(tool, [[s["id"]]])  # where a stub from this tool belongs
    return [[s["id"]]], s


def grant(g: Graph, who, scope_rec, via: str, level: str = "read", method: str = "snapshot", **dates):
    """CAN_ACCESS from an Account, Group or Workspace to a place's AccessScope."""
    g.link("CAN_ACCESS", who, scope_rec, method, level=level, via=via, **dates)
