"""Adapters that turn a dataset folder (one sub-folder per source tool) into EKG records and links.

One module per source (people, jira, github, git, mail, wiki), a shared core
(common: envelope, time, text, validation; access: scopes), the cross-source linker
and identity resolution. pipeline.run() runs them in order; build_graph.py is the CLI.
"""
