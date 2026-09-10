# job-fetch

Pulls finance job postings for Singapore from public company career APIs
(Greenhouse, Lever, Ashby, SmartRecruiters, Workday) and MyCareersFuture every
weekday morning, filters them by title and location, and publishes the day's new
postings under `digests/latest/` for a separate scorer to read.

Contains no personal data: a script, a company list, a title filter, and job postings.
All sources are public APIs the companies publish for exactly this purpose.

- `digests/latest/index.json` — date, count, number of parts, source health
- `digests/latest/part-NN.json` — the postings, twelve per file
- `state.json` — hashes of postings already published, so nothing repeats
