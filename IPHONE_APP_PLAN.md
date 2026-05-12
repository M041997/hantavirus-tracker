# iPhone app — pandemic surveillance (planning notes)

Discussed 2026-05-12. Intent: build this on the Mac (Xcode is Mac-only). This file
exists so Mac-side Claude can pick up the thread.

## Why an app

The hantavirus tracker may lose relevance if the Hondius outbreak fizzles. A more
durable product is a pandemic-agnostic surveillance app — useful whenever the next
outbreak happens, not tied to one pathogen.

## Two product directions

1. **Generalize the existing tracker** to multi-pathogen.
   - ProMED + WHO DON scrapers already cover any disease; mostly a matter of
     removing the hantavirus-specific filter and the hardcoded Hondius vessel.
   - Cheap, additive, stays in this repo.
   - Web-first; the iOS app would just render the same JSON.

2. **Wastewater surveillance aggregator** (new project).
   - CDC NWSS + Biobot + WastewaterSCAN all publish data; nobody has stitched
     them into a consumer "what's circulating in my zip" view.
   - Stronger differentiator: wastewater leads official case counts, and trust
     in case counts dropped after COVID.
   - Different data sources from this repo — would be a separate codebase.

Not mutually exclusive; could ship #1 first and add #2.

## App Store policy risk (load-bearing)

Apple guideline 5.1.1(ix) restricts apps with contagious-disease info to
recognized institutions: governments, accredited medical orgs, health NGOs.
A solo-dev "pandemic tracker" can get rejected at review.

Mitigations:
- Frame as a **public-data visualization tool** (e.g. "CDC NWSS dashboard"),
  not health guidance.
- Show data, don't interpret risk. No "should I mask" / "is it safe" UX.
- Skip outbreak push notifications — that triggers the strictest review path.

If App Store rejection is likely, fallback is TestFlight-only distribution.

## Suggested stack

- **iOS:** SwiftUI, single target, iOS 17+.
- **Backend:** thin JSON API. Either:
  - Reuse this repo's GitHub Pages output (`docs/data.json` is already served), or
  - Stand up a real API on Cloudflare Workers / Fly / Render if we need
    auth, per-user state, or non-static endpoints.
- **Scraping:** reuse the Python in `build.py`. The phone stays dumb; data logic
  updates without app-store resubmits.

## Open decisions for the user

- Pivot direction: generalize-tracker, wastewater-aggregator, or both.
- Backend: reuse GitHub Pages JSON, or stand up a real API.
- Free vs. paid. App Store vs. TestFlight-only initially.
- App name / brand. Probably not "hantavirusonline" if it's multi-pathogen.

## Picking this up on the Mac

1. Confirm direction with the user (the three open decisions above).
2. Scaffold the Xcode project — confirm before installing Xcode / command line
   tools / Homebrew packages (per the user's standing "new Mac, extra caution" rule).
3. If reusing this repo's JSON, the URL is `https://hantavirusonline.org/data.json`
   (regenerated hourly by the GitHub Action).
