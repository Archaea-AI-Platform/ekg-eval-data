"""Conversations pillar: mailing-list mbox archives -> MailingList, EmailMessage, EmailThread, EmailAddress.

Classifies each message: a Jira notification ANNOUNCES its ticket (a notice, not a new
fact); a human message that names a ticket key DISCUSSES it; a [DISCUSS] / [VOTE]
thread about KIP-n is where that decision was made (MADE_IN, queued for the linker).
Cross-posted copies are one message IN each list. Senders the list rewrote to
"Name via list" keep no address, so they get no FROM link.
"""

import datetime as dt
import email.header
import email.utils
import mailbox
import re
from pathlib import Path

from .access import place
from .common import Graph, from_dt, ticket_rx

JIRA_NOTICE = re.compile(r"^\[jira\] \[(\w+)\] \(([A-Z][A-Z0-9]+-\d+)\)")
KIP_THREAD = re.compile(r"\[(DISCUSS|VOTE|RESULT)\].*?\bKIP-(\d+)\b", re.I)
AUTOMATED = {"jira@apache.org": "jira", "git@apache.org": "github"}


def header(value):
    return str(email.header.make_header(email.header.decode_header(str(value or "")))).replace("\n", " ").strip()


def plain_text(m):
    if not m.is_multipart():
        payload = m.get_payload(decode=True) or b""
    else:
        payload = next((pt.get_payload(decode=True) for pt in m.walk() if pt.get_content_type() == "text/plain"), b"") or b""
    return payload.decode(m.get_content_charset() or "utf-8", "replace")


def map_mail(g: Graph, d: Path):
    key_rx = ticket_rx(g)
    lists, messages, addresses, threads, kip_threads = {}, g.ctx.setdefault("emails", {}), {}, {}, set()
    boxes = sorted((d / "mail").glob("*/*.mbox"))
    for mb in boxes:
        addr = mb.parent.name
        if addr not in lists:
            scope, _ = place(g, "mail", "mailing_list", addr)
            lists[addr] = g.rec("MailingList", "mail", addr, {"address": addr, "kind": "public_list"}, scope)
        lst = lists[addr]
        for m in mailbox.mbox(str(mb)):
            mid = (m["Message-ID"] or "").strip()
            if mid in messages:  # cross-posted: one message, in each list
                g.notes["mail: cross-posted copy merged into one message"] += 1
                msg = messages[mid]
                if lst["scope"][0] not in msg["scope"]:
                    msg["scope"] = [msg["scope"][0] + lst["scope"][0]]  # visible through either list
                g.link("IN", msg, lst, "field", valid_from=msg["props"]["sent_at"])
                g.link("SENT_TO", msg, lst, "field", role="to", at=msg["props"]["sent_at"])
                continue
            if m["Date"]:
                sent = from_dt(email.utils.parsedate_to_datetime(m["Date"]))
            else:
                g.notes["mail: no Date header (archive receive time used, precision approximate)"] += 1
                sent = dt.datetime.strptime(" ".join(m.get_from().split()[-5:]), "%a %b %d %H:%M:%S %Y").strftime("%Y-%m-%dT%H:%M:%SZ")
            subject = header(m["Subject"])
            msg = g.rec("EmailMessage", "mail", mid, {"message_id": mid, "subject": subject, "sent_at": sent,
                        "sent_at_precision": None if m["Date"] else "approximate", "text": plain_text(m)}, [list(lst["scope"][0])])
            messages[mid] = msg
            g.link("IN", msg, lst, "field", valid_from=sent)
            g.link("SENT_TO", msg, lst, "field", role="to", at=sent)

            name, sender = email.utils.parseaddr(header(m["From"]))
            sender = sender.lower()
            refs = (m["References"] or "").split()
            parent = (m["In-Reply-To"] or "").strip()
            root = refs[0] if refs else (parent or mid)
            if root not in threads:
                threads[root] = g.rec("EmailThread", "mail", root, {"root_message_id": root,
                                      "normalized_subject": re.sub(r"^((re|fwd?|aw):\s*)+", "", subject, flags=re.I)},
                                      [list(lst["scope"][0])])
            g.link("IN", msg, threads[root], "field", valid_from=sent)

            if not sender:
                g.notes["mail: From header with no parseable address"] += 1
            elif sender in lists:
                g.notes["mail: sender rewritten to the list address (no FROM link; display name only)"] += 1
            else:
                if sender not in addresses:
                    addresses[sender] = g.rec("EmailAddress", "email", sender, {"address": sender, "display_name": name or None,
                                              "normalized_value": sender, "kind": "automation" if sender in AUTOMATED else None},
                                              lst["scope"])
                g.link("FROM", msg, addresses[sender], "field", role="sender", at=sent)
                g.link("PARTICIPATED_IN", addresses[sender], threads[root], "field", role="replied" if parent else "started",
                       valid_from=sent)

            # classify
            notice = JIRA_NOTICE.match(subject)
            if sender == "jira@apache.org" and notice:
                g.ref(msg, "ANNOUNCES", "Ticket", notice.group(2), "integration", stub=True, kind="notify", at=sent)
                g.notes["mail: Jira notifications (ANNOUNCES their ticket)"] += 1
            elif sender == "git@apache.org":
                g.notes["mail: GitHub notifications for another repo (kafka-site), not linked"] += 1
            else:
                for k in sorted(set(key_rx.findall(subject))):
                    g.ref(msg, "DISCUSSES", "Ticket", k, "text_rule", 0.9, kind="discuss", at=sent)
                kt = KIP_THREAD.search(subject)
                if kt and root not in kip_threads:  # once per thread: its first message in the archive
                    kip_threads.add(root)
                    g.ref(msg, "MADE_IN", "Decision", int(kt.group(2)), "text_rule", 0.8, reverse=True, at=sent)
    done = set()
    for mb in boxes:
        for m in mailbox.mbox(str(mb)):
            mid, parent = (m["Message-ID"] or "").strip(), (m["In-Reply-To"] or "").strip()
            if mid in done:
                continue
            done.add(mid)
            if parent in messages:
                g.link("REPLY_TO", messages[mid], messages[parent], "field")
            elif parent:
                g.notes["mail: reply whose parent is outside the window"] += 1
