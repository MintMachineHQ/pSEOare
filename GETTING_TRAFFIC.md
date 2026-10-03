# Getting Traffic — what to do when you wake up

Everything technical already works. This file is about the three things only you can do.
No code required. Total time: about 20 minutes for steps 1 and 2.

**The problem in one sentence:** Google and Bing have never seen this site, so nobody
visits, so the ads earn nothing. Ads make money from *visitors*, not from pages.

---

## 1. Bing Webmaster Tools — done, you only need to look

**Already working, no action needed:**

- The site is verified and the API key is set, so the daily build now spends Bing's
  submission allowance itself, right after it publishes. It reads the day's remaining
  quota, submits that many long-tail pages, and stops cleanly. You never have to
  remember to press a button.
- Because the allowance is only **100 URLs a day** against a corpus of 2,200+, the tool
  deliberately submits the individual data pages first and leaves the hub indexes and
  the homepage for last. Hubs are reachable from any deep link; the specific pages are
  the ones that can win a query.
- Automatic IndexNow pings still run for every new page, so nothing waits on the quota.

**The only thing worth doing is looking.** Go to <https://www.bing.com/webmasters> and
check **URL Inspection** or **IndexNow** after a few days. That dashboard is the fastest
honest read on whether Bing is taking the pages.

---

## 2. Search Console → Request Indexing (about 5 minutes, do this one first)

**What it is:** a button that tells Google "crawl this exact page now" instead of
waiting for it to find you on its own.

**Why it matters most:** Google has indexed nothing yet. Requesting the hub pages gets
the biggest pages indexed fastest, and Google then follows the links inside them to the
rest.

**Do this:**

1. Go to <https://search.google.com/search-console> (already verified).
2. In the search bar at the top, paste a URL from the list below and press Enter.
3. Click **Request Indexing**, wait for the confirmation.
4. Repeat for each of the 10.

The 9 to request, in order of importance. All of these were checked and return 200 —
copy them one at a time. Google allows roughly 10–12 requests a day per property, so
this is a two-minute job, not an all-day one.

```
https://pseoare.pages.dev/
https://pseoare.pages.dev/hub-countries
https://pseoare.pages.dev/hub-climate
https://pseoare.pages.dev/hub-crypto
https://pseoare.pages.dev/hub-single-holidays
https://pseoare.pages.dev/afghanistan-country-data
https://pseoare.pages.dev/amsterdam-average-monthly-temperature-rainfall
https://pseoare.pages.dev/bitcoin-price-by-month
https://pseoare.pages.dev/amazigh-new-year-morocco-2024-01-14
```

**If you want more than these 9,** open
`https://pseoare.pages.dev/sitemap.xml`, copy any `<loc>` line, and request that instead.
Do not guess a URL — the site returns a real 404 for anything that does not exist, and a
404 tells Google nothing.

**A note on the corpus size.** There are over 2,200 pages live and it grows by roughly
150 a day. Do not try to request them all — Google will throttle you and the hubs do the
same job for the rest. Request the hubs, then spot-check a handful of individual pages
once a week. If a specific country or city page is missing that you expected, that is a
real gap worth reporting; a page that has not been generated yet is not.

**Expect a "not indexed" verdict on most of them.** That is normal and not a
rejection — it means "not yet", and Request Indexing still queues the crawl. Only if it
says *Crawled - currently not indexed* repeatedly for 2 weeks is there a real problem,
and by then the content will have had time to prove itself.

---

## 3. The CPA offer — already handled, here's why

**What it was meant to be:** the top and middle ad slots were placeholders pointing at
`example.com`, a reserved dummy domain. They earned nothing and sent visitors nowhere.

**What I did instead:** they now show a house ad — a small link to our own dataset
index. I picked that over an affiliate offer for three reasons:

1. It costs nothing and needs no approval.
2. It keeps people moving through the site. Every internal click is one more exit from a
   page, and every exit is one more chance for the popunder to fire.
3. Most CPA networks will not approve a brand-new site with no traffic anyway.

**How to add a real offer later,** when you have one: send me the offer URL and a
728×90 image URL and I will point those slots at it. It is a two-line config change.

---

## 4. Inbound links — the honest part

**What they are:** other websites linking to ours. They are the single biggest factor in
ranking, and no amount of code produces them.

**What I cannot do:** create them. I cannot post to forums, submit to link directories,
or run outreach as you, because all of that needs accounts in your name, and most of it
is against platform rules when done automatically. Bulk link buying is worse than
useless here — Google discounts it and Adsterra's own quality checks would flag it.

**What actually works, roughly in order of effort:**

| Option | Cost | Effort | Realistic impact |
| --- | --- | --- | --- |
| Custom domain instead of `pages.dev` | ~$10/year | 20 min | Moderate. A real domain is trusted more than a free subdomain. |
| Post the data somewhere people already ask these questions | Free | Ongoing | High. One good thread can out-earn months of waiting. |
| Add the site to a few genuine directories | Free | 30 min | Low but free. |
| Guest post or get a link from a related site | Free | Hours | High, slow. |
| Buy links or use an SEO service | $100+ | — | Avoid. Risks a manual action and an Adsterra ban. |

**The two I would actually do:**

1. **Get a custom domain.** A ~$10 `.com` pointed at Cloudflare Pages. It signals
   permanence, it can be moved to another host, and it stops the whole project living on
   a URL you do not own. I can set up the Cloudflare side; you buy the domain.
2. **Answer one real question, once, somewhere public.** Our Berlin climate page has a
   table of monthly temperatures. Someone asking "what is the weather like in Berlin in
   July" in a forum or a Reddit thread is exactly the person this page was built for.
   Link it as a genuine answer. Do this a dozen times over a few months and that is the
   traffic.

---

## What is already running without you

- Daily rebuild at 04:17 UTC, published automatically
- Cloudflare mirror, so the live site updates itself
- IndexNow pings to Bing on every changed page
- AI prose enrichment, Mistral first (Cerebras → Mistral → Groq → Gemini)
- A daily revenue report that tells you impressions, clicks and money, and names any ad
  unit sitting at zero

None of that needs you. The list above is the part that does.
