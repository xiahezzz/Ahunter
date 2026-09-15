---
status: accepted
---

# Never overwrite published Team versions

Publishing a new Team identity creates version 1, while publishing a revision assigns the next integer version atomically. The WebUI never accepts a version number and never overwrites a published manifest, even when that version has not yet appeared in a Research Run; abandoned drafts create no manifest. Published versions also have no delete or archive lifecycle and remain available as immutable history.
