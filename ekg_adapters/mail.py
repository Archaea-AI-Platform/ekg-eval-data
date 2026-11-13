"""Conversations pillar: mailing-list mbox archives -> MailingList, EmailMessage, EmailThread, EmailAddress.

Reads every .mbox under mail/<folder>/ (any file name) or directly under mail/. The list address is the
manifest's settings.mail_lists[folder], else the messages' List-Id header, else the folder name.

Classifies each message, with the senders and patterns from the project's settings:
  - a tracker notification ("[jira] [Created] (KEY-1) ...") ANNOUNCES its ticket
  - a code-host notification (a "URL: https://github.com/<repo>/pull/N" or ".../issues/N" line) ANNOUNCES
    that PR or issue when it is the project's repo; a notice for another repo, and CI notices, are counted
  - a person's message DISCUSSES the ticket keys in its subject, MENTIONS those only in its body, LINKS_TO
    the PRs and issues it links, and a [DISCUSS] / [VOTE] thread about a decision key is where that
    decision was made (MADE_IN)
A message that can't be read is skipped and counted with its reason; one without a Message-ID gets a
stable generated one. The same message twice in one list is kept once; in two lists it is one message
IN both. Senders the list rewrote to "Name via list" keep no address, so they get no FROM link.
"""

import datetime as dt
import email.errors
import email.header
import email.utils
import hashlib
import mailbox
import re
from pathlib import Path

from .access import place
from .common import Graph, from_dt, ticket_rx

JIRA_NOTICE = re.compile(r"^\[jira\] \[(\w+)\] \(([A-Z][A-Z0-9]+-\d+)\)")
GH_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(pull|issues)/(\d+)")
GH_REF = re.compile(r"(?<![\w/&#])#(\d{1,6})\b")
# a vote in a reply: "+1 (binding)", "Thanks! +1 (non-binding).", "-1"
VOTE_LINE = re.compile(r"(?<![\w+-])([+-][01])(?![\d.])([^\n]{0,60})")
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12}
TALLY = re.compile(r"\b(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")\s+(non[- ]?binding|binding)\s*(?:\+1|votes?|\(\+1)", re.I)


def header(value):
    try:
        return str(email.header.make_header(email.header.decode_header(str(value or "")))).replace("\n", " ").strip()
    except (UnicodeError, LookupError, email.errors.HeaderParseError):
        return str(value or "").replace("\n", " ").strip()


def plain_text(m):
    if not m.is_multipart():
        payload = m.get_payload(decode=True) or b""
    else:
        payload = next((pt.get_payload(decode=True) for pt in m.walk() if pt.get_content_type() == "text/plain"), b"") or b""
    try:
        return payload.decode(m.get_content_charset() or "utf-8", "replace")
    except LookupError:  # an unknown charset name
        return payload.decode("utf-8", "replace")


def _sent(m):
    try:
        return from_dt(email.utils.parsedate_to_datetime(m["Date"])), None
    except (TypeError, ValueError, IndexError):
        pass
    try:  # some exporters write an ISO 8601 date
        return from_dt(dt.datetime.fromisoformat((m["Date"] or "").strip().replace("Z", "+00:00"))), None
    except (TypeError, ValueError, AttributeError):
        pass
    try:  # the mbox envelope line carries the archive's receive time
        return dt.datetime.strptime(" ".join(m.get_from().split()[-5:]), "%a %b %d %H:%M:%S %Y").strftime("%Y-%m-%dT%H:%M:%SZ"), "approximate"
    except (ValueError, AttributeError, TypeError):
        return None, None


def _list_address(m):
    lid = header(m["List-Id"])
    hit = re.search(r"<([^>]+)>", lid)
    return hit.group(1).replace(".", "@", 1) if hit else None


def _message_id(m, subject, sent):
    mid = (m["Message-ID"] or "").strip()
    if mid:
        return mid, False
    seed = f"{header(m['From'])}|{sent}|{subject}|{plain_text(m)[:200]}"
    return f"<generated-{hashlib.sha1(seed.encode()).hexdigest()[:20]}@ekg>", True


def map_mail(g: Graph, d: Path):
    s = g.ctx["settings"]
    key_rx = ticket_rx(g)
    repo = (g.ctx.get("repo_name") or "").lower()
    notifiers = {k.lower(): v for k, v in s["notifiers"].items()}
    ci_rx = re.compile(s["ci_subject"], re.I)
    decision_rx = re.compile(rf"\[(DISCUSS|VOTE|RESULT)\].*?\b{re.escape(s['decision_prefix'])}-(\d+)\b", re.I) \
        if s.get("decision_prefix") else None
    github_issues = s.get("issue_tracker") == "github"
    lists, messages, addresses, threads, decided = {}, g.ctx.setdefault("emails", {}), {}, {}, set()
    in_list = set()  # (message id, list address): the same copy twice in one list is one copy
    boxes = sorted((d / "mail").glob("*/*.mbox")) + sorted((d / "mail").glob("*.mbox"))
    parents = []
    for mb in boxes:
        folder = mb.parent.name if mb.parent != d / "mail" else mb.stem
        try:
            box = list(mailbox.mbox(str(mb)))
        except (OSError, UnicodeError) as e:
            g.notes[f"mail: unreadable mbox skipped ({type(e).__name__})"] += 1
            continue
        for m in box:
            try:
                _one(g, m, folder, s, lists, messages, addresses, threads, decided, in_list, parents, key_rx, repo,
                     notifiers, ci_rx, decision_rx, github_issues)
            except Exception as e:  # one bad message never stops the run
                g.notes[f"mail: message skipped ({type(e).__name__})"] += 1
    for mid, parent in parents:
        if parent in messages:
            g.link("REPLY_TO", messages[mid], messages[parent], "field")
        elif parent:
            g.notes["mail: reply whose parent is outside the window"] += 1


def _one(g, m, folder, s, lists, messages, addresses, threads, decided, in_list, parents, key_rx, repo,
         notifiers, ci_rx, decision_rx, github_issues):
    addr = s["mail_lists"].get(folder) or _list_address(m) or folder
    if addr not in lists:
        scope, _ = place(g, "mail", "mailing_list", addr)
        lists[addr] = g.rec("MailingList", "mail", addr, {"address": addr, "kind": "public_list"}, scope)
    lst = lists[addr]
    subject = header(m["Subject"])
    sent, precision = _sent(m)
    if sent is None:
        g.notes["mail: message skipped (no readable date)"] += 1
        return
    mid, generated = _message_id(m, subject, sent)
    if (mid, addr) in in_list:
        g.notes["mail: duplicate copy in the same list, kept once"] += 1
        return
    in_list.add((mid, addr))
    if mid in messages:  # cross-posted: one message, in each list
        g.notes["mail: cross-posted copy merged into one message"] += 1
        msg = messages[mid]
        if lst["scope"][0] not in msg["scope"]:
            msg["scope"] = [msg["scope"][0] + lst["scope"][0]]  # visible through either list
        g.link("IN", msg, lst, "field", valid_from=msg["props"]["sent_at"])
        g.link("SENT_TO", msg, lst, "field", role="to", at=msg["props"]["sent_at"])
        return
    if generated:
        g.notes["mail: no Message-ID (stable id generated)"] += 1
    if precision:
        g.notes["mail: no Date header (archive receive time used, precision approximate)"] += 1
    body = plain_text(m)
    msg = g.rec("EmailMessage", "mail", mid, {"message_id": mid, "subject": subject, "sent_at": sent,
                "sent_at_precision": precision, "text": body}, [list(lst["scope"][0])])
    messages[mid] = msg
    g.link("IN", msg, lst, "field", valid_from=sent)
    g.link("SENT_TO", msg, lst, "field", role="to", at=sent)

    name, sender = email.utils.parseaddr(header(m["From"]))
    sender = sender.lower()
    if m["From"]:  # the From header as sent ("Chia-Ping Tsai (Jira) via dev <dev@kafka.apache.org>"): one sender identity
        msg["props"]["from_header"] = (f'{name} <{sender}>' if name else sender) if sender else header(m["From"])[:300]
    refs = (m["References"] or "").split()
    parent = (m["In-Reply-To"] or "").strip()
    parents.append((mid, parent))
    root = refs[0] if refs else (parent or mid)
    if root not in threads:
        threads[root] = g.rec("EmailThread", "mail", root, {"root_message_id": root,
                              "normalized_subject": re.sub(r"^((re|fwd?|aw):\s*)+", "", subject, flags=re.I)},
                              [list(lst["scope"][0])])
    g.link("IN", msg, threads[root], "field", valid_from=sent)

    if name and not msg["props"].get("sender_name"):  # the display name as sent ("Ismael Juma (Jira)")
        msg["props"]["sender_name"] = re.sub(r"\s+via\s+\S+$", "", name).strip().strip('"') or None
    for cc_name, cc in email.utils.getaddresses([header(m["Cc"])] if m["Cc"] else []):
        cc = cc.lower()
        if cc and "@" in cc and cc not in lists:
            if cc not in addresses:
                addresses[cc] = g.rec("EmailAddress", "email", cc, {"address": cc, "display_name": cc_name or None,
                                      "normalized_value": cc}, lst["scope"])
            g.link("SENT_TO", msg, addresses[cc], "field", role="cc", at=sent)
    tool = notifiers.get(sender)
    if not sender:
        g.notes["mail: From header with no parseable address"] += 1
    elif sender in lists:  # the list rewrote From to itself: only the display name is the sender's
        name = re.sub(r"\s+via\s+\S+$", "", name or "").strip().strip('"')
        if name:
            msg["props"]["sender_name"] = name
        msg["props"]["self_reported_sender"] = True  # the list relayed it: only the display name says who sent it
        g.notes["mail: sender rewritten to the list address (sender_name kept, no FROM link)"] += 1
    else:
        if sender not in addresses:
            addresses[sender] = g.rec("EmailAddress", "email", sender, {"address": sender, "display_name": name or None,
                                      "normalized_value": sender, "kind": "automation" if tool else None}, lst["scope"])
        g.link("FROM", msg, addresses[sender], "field", role="sender", at=sent)
        g.link("PARTICIPATED_IN", addresses[sender], threads[root], "field", role="replied" if parent else "started",
               valid_from=sent)

    # ---- notifications from integrations
    if tool == "jira":
        notice = JIRA_NOTICE.match(subject)
        if notice:
            g.ref(msg, "ANNOUNCES", "Ticket", notice.group(2), "integration", stub=True, kind="notify", at=sent)
            g.notes["mail: tracker notifications (ANNOUNCES their ticket)"] += 1
        else:
            g.notes["mail: tracker notifications with no ticket key, not linked"] += 1
        return
    if tool == "ci" or (tool == "github" and ci_rx.search(subject)):
        g.notes["mail: CI notifications (noise, not linked)"] += 1
        return
    if tool == "github":
        hits = [h for h in GH_URL.findall(f"{subject}\n{body}") if h[0].lower() == repo]
        if not hits:
            g.notes["mail: code-host notifications for another repo, not linked"] += 1
            return
        _, what, num = hits[0]
        target = "PullRequest" if what == "pull" else "GitHubRef"
        g.ref(msg, "ANNOUNCES", target, int(num), "integration", stub=True, kind="notify", at=sent)
        g.notes["mail: code-host notifications (ANNOUNCES their PR or issue)"] += 1
        return

    # ---- a person's message
    subject_keys = set(key_rx.findall(subject))
    for k in sorted(subject_keys):
        g.ref(msg, "DISCUSSES", "Ticket", k, "text_rule", 0.9, kind="discuss", at=sent)
    quoted = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))  # not the quoted reply
    for k in sorted(set(key_rx.findall(quoted)) - subject_keys):
        g.ref(msg, "MENTIONS", "Ticket", k, "text_rule", 0.8, kind="key", at=sent)
    for r, what, num in set(GH_URL.findall(quoted)):
        if r.lower() == repo:
            g.ref(msg, "LINKS_TO", "PullRequest" if what == "pull" else "GitHubRef", int(num), "field", 0.95,
                  kind="url", at=sent)
    if github_issues:  # the subject names what the thread is about; the body (not its quoted part) mentions
        subject_nums = set(GH_REF.findall(subject))
        for num in sorted(subject_nums, key=int):
            g.ref(msg, "MENTIONS", "GitHubRef", int(num), "text_rule", 0.7, kind="key", at=sent)
        for num in sorted(set(GH_REF.findall(quoted)) - subject_nums, key=int):
            g.ref(msg, "MENTIONS", "GitHubRef", int(num), "text_rule", 0.6, kind="key", at=sent)
    if decision_rx:
        dt_hit = decision_rx.search(subject)
        if dt_hit and root not in decided:  # once per thread: its first message in the archive
            decided.add(root)
            g.ref(msg, "MADE_IN", "Decision", int(dt_hit.group(2)), "text_rule", 0.8, reverse=True, at=sent)
        if dt_hit and dt_hit.group(1).upper() in ("VOTE", "RESULT") and s.get("vote_threads"):
            _votes(g, msg, int(dt_hit.group(2)), quoted, addresses.get(sender) if sender not in lists else None, sent)


def _votes(g, msg, kip, text, voter, sent):
    """A [VOTE] thread: a reply that votes makes its sender a voter on the decision (DECIDED_BY role voter, with
    the vote and whether it binds); a message that states the tally ("4 binding +1 and 1 non-binding +1") sets
    it on the decision (the latest such message wins)."""
    _acceptance(g, msg, kip, text, voter, sent)
    tally = {}
    for n, kind in TALLY.findall(text):
        key = "non_binding_votes" if kind.lower().startswith("non") else "binding_votes"
        tally.setdefault(key, int(n) if n.isdigit() else NUMBER_WORDS[n.lower()])
    if tally:
        tallies = g.ctx.setdefault("kip_tally", {})
        if sent and (kip not in tallies or (tallies[kip]["at"] or "") <= sent):
            tallies[kip] = {**tally, "at": sent}
        g.notes["mail: KIP vote tallies read from a vote thread"] += 1
        return  # a message that reports the tally does not cast a vote
    head = []  # the reply's own words, before the quoted message ("On ... wrote:")
    for line in text.strip().splitlines()[:12]:
        if re.match(r"\s*On .{4,}", line) and ("wrote" in line or len(head) > 1):
            break
        head.append(line)
    m = VOTE_LINE.search("\n".join(head))
    if not m:
        return
    if voter is None:  # the list rewrote From: the sender's name is all there is
        voter = _voter_by_name(g, msg["props"].get("sender_name"), msg)
        if voter is None:
            return
    note = m.group(2).lower()
    binding = False if re.search(r"non[- ]?bin?d", note) else True if re.search(r"\bbi\w{0,3}ing\b", note) else None
    g.ref(voter, "DECIDED_BY", "Decision", kip, "text_rule", 0.9, reverse=True, evidence=[msg], role="voter",
          vote=m.group(1), binding=binding, at=sent)
    g.notes["mail: KIP votes cast in a vote thread (DECIDED_BY role voter)"] += 1


def _voter_by_name(g, name, msg):
    """A voter known only by display name: the account of that exact name when one exists (a Jira account, say),
    else an Alias holding the name."""
    if not name:
        return None
    index = g.ctx.get("_accounts_by_name")
    if index is None:
        index = g.ctx["_accounts_by_name"] = {}
        for r in g.records.values():
            if r["type"] == "Account" and r["props"].get("display_name"):
                index.setdefault(r["props"]["display_name"].strip().lower(), r)
    hit = index.get(name.strip().lower())
    if hit:
        g.notes["mail: voter matched to an account by display name"] += 1
        return hit
    g.notes["mail: voter kept as a display-name alias"] += 1
    return g.rec("Alias", "email", f"display_name:{name.strip().lower()}", {"value": name.strip(), "kind": "display_name",
                 "namespace": "email", "normalized_value": name.strip().lower()}, msg["scope"])


ACCEPTED = re.compile(r"\b(?:has|have) (?:now )?been accepted\b|\bis (?:now )?accepted\b|\bthe vote (?:has )?passed\b|"
                      r"\bvote passes\b|\bhas passed the vote\b", re.I)
WITHDRAWN = re.compile(r"\bkeep (?:it|the vote|the thread) open\b|\bchange the status\b|\bretract|\bwithdraw|"
                       r"\b(?:re-?open)\b|\bnot (?:yet )?(?:be )?accepted\b|\bpremature", re.I)


def _acceptance(g, msg, kip, text, voter, sent):
    """In a vote thread, the one who announced the proposal accepted and later withdrew it (a vote closed too early,
    say): the withdrawal is a Decision of its own that REVERSES the proposal's acceptance, from that message until
    an acceptance announced again."""
    who = voter or _voter_by_name(g, msg["props"].get("sender_name"), msg)
    if who is None or not sent:
        return
    head = "\n".join(text.strip().splitlines()[:15])
    state = g.ctx.setdefault("kip_acceptance", {}).setdefault(kip, {})
    if ACCEPTED.search(head):
        if state.get("withdrawn") is not None:  # accepted again: the reversal ends here
            state["withdrawn"]["valid_to"] = sent
            state["withdrawn"] = None
        state["by"], state["at"] = who["id"], sent
        return
    own = (g.ctx.get("settings") or {}).get("withdrawal_phrases")
    withdrawn = re.compile(own, re.I) if own else WITHDRAWN
    if state.get("by") == who["id"] and state.get("at") and state["at"] < sent and withdrawn.search(head):
        line = next((ln.strip() for ln in head.splitlines() if withdrawn.search(ln)), head[:200])
        dec = g.rec("Decision", "derived", f"kip:{kip}:withdrawn:{msg['props']['message_id']}", {
            "title": f"Acceptance of KIP-{kip} withdrawn",
            "statement": f"{msg['props'].get('sender_name') or 'The announcer'} withdrew the acceptance of KIP-{kip} "
                         f"announced {state['at']}: \"{line}\"",
            "rationale": head[:1500], "status": "accepted", "decided_at": sent}, msg["scope"])
        g.link("MADE_IN", dec, msg, "text_rule", 0.85, evidence=[msg], at=sent)
        g.link("DECIDED_BY", dec, who, "text_rule", 0.85, evidence=[msg], role="decider", at=sent)
        g.ref(dec, "REVERSES", "Decision", kip, "text_rule", 0.85, evidence=[msg], valid_from=sent, kind="withdrawal",
              reason=line[:300])
        state["withdrawn"] = g.refs[-1][7]  # the queued link's own fields: valid_to is set if it is accepted again
        g.notes["mail: proposal acceptances withdrawn in the vote thread (Decision REVERSES)"] += 1
