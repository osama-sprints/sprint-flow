"""Documents the agent reads on demand — today, PDFs.

A PDF is stored once (in Mattermost) and opened here per request. The agent
inspects it, searches what text is available, and reads pages or ranges;
pages without a usable text layer are rendered and transcribed by a vision
model only when they are asked for. Every result names the file, the
document id, the physical page and its printed label, how the text was
obtained, and what was not covered.
"""
