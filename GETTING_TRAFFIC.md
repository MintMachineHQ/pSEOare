# Getting Traffic — what to do when you wake up

Everything technical already works. This file is about the three things only you can do.
No code required. Total time: about 20 minutes for steps 1 and 2.

**The problem in one sentence:** Google and Bing have never seen this site, so nobody
visits, so the ads earn nothing. Ads make money from *visitors*, not from pages.

---

## 1. Bing Webmaster Tools (about 10 minutes)

**What it is:** a free Microsoft tool where you prove you own a website and then watch
it get crawled and indexed. Google has the same thing (Search Console — already done).
Bing is easier to get indexed on, and it feeds a lot of other sites.

**What you already have:** automatic pings to Bing are switched on and working. I tested
one live and Bing accepted it. So the plumbing is done — you just need the dashboard to
see it and to push harder.

**Do this:**

1. Go to <https://www.bing.com/webmasters>
2. Sign in with a Microsoft account (Outlook / Hotmail / live.com). Use a new one if you
   want to keep this separate.
3. Click **Add site**, type `pseoare.pages.dev`, submit.
4. Bing gives you a choice of how to prove ownership. Pick **Import from GSC** if it
   offers that (you already verified Search Console, so it is instant). Otherwise pick
   **XML file upload** and it will show you a filename that looks like
   `BingSiteAuth.xml`.
5. Copy that exact filename and its contents and paste them into `config.json` in this
   block:

```json
"verification_files": {
  "googleeba8f39aab451061.html": "google-site-verification: googleeba8f39aab451061.html",
  "BingSiteAuth.xml": "PASTE_THE_CONTENTS_BING_GAVE_YOU_HERE"
}
```

6. Commit and push, or just tell me to do it. The file goes live on the next build
   (daily at 04:17 UTC, or immediately if you say so).
7. Back in Bing, click **Verify**. Then submit the sitemap:
   `https://pseoare.pages.dev/sitemap.xml`

You do not need to do step 5 yourself — send me the filename and contents and I will
publish it in one commit.

---

## 2. Search Console → Request Indexing (about 5 minutes, do this one first)

**What it is:** a button that tells Google "crawl this exact page now" instead of
waiting for it to find you on its own.

**Why it matters most:** you have 238 pages and Google has indexed zero. Requesting the
10 hub pages gets the biggest pages indexed fastest, and Google then follows the links
inside them to the rest.

**Do this:**

1. Go to <https://search.google.com/search-console> (already verified).
2. In the search bar at the top, paste a URL from the list below and press Enter.
3. Click **Request Indexing**, wait for the confirmation.
4. Repeat for each of the 10.

The 9 to request, in order of importance. All of these were checked and return 200 —
copy them one at a time:

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

**A note on the corpus size.** There are 238 pages live, not the full planned set. The
build adds roughly 300 more per day and only rewrites what changed, so the site fills
out over the next few weeks. There is no `japan-country-data` page yet — that is normal,
not a fault.

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
- AI prose enrichment, Groq first
- A daily revenue report that tells you impressions, clicks and money, and names any ad
  unit sitting at zero

None of that needs you. The list above is the part that does.
