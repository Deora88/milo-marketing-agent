# Milo — Privacy Policy and Terms

_Last updated: 18 October 2026_

Milo is a student internship project: a conversational AI agent that drafts, publishes (after human approval) and
monitors Meta ad campaigns for the account owner who runs it. It is not a public service and has no end users other
than the person running it on their own computer.

## What data Milo uses
- **Meta ad account data** of the account owner only: campaigns, ad sets, ads and their performance numbers,
  read and written through the Meta Marketing API with the owner's own access token.
- **Chat messages** typed into Milo, sent to the configured AI model provider (OpenRouter) to generate replies.
- **Web search queries** (business type and city) sent to Tavily to find recent context for ad copy.
- **Image prompts** (a scene description, no personal data) sent to Cloudflare Workers AI, where the FLUX.1
  [schnell] model generates the ad image.

## Where data is stored
Campaigns, approvals, metrics and the activity log are stored in the team's MongoDB database (MongoDB Atlas).
Uploaded ad images are stored in the local `data/` folder and sent only to Meta. Access tokens and API keys stay in
a local `.env` file and are never sent to the browser or to any third party other than the API they belong to.

## Sharing
Milo does not sell, share or transfer data to anyone. It collects no data from Facebook or Instagram users and uses
no Facebook Login.

## Deletion
Drop the `milo` database in MongoDB Atlas (and delete the `data/` folder) to remove all stored data. Remove the app in Meta Business Settings to revoke its access. For
questions, contact the developer at the email listed in the Meta app settings.

## Terms of use
Milo is provided as-is for testing and evaluation with the team's Meta business ad account. Spending is off by default,
so campaigns stay paused until the account owner enables it. Every change to a live ad requires
the owner's explicit approval in the Milo dashboard. The owner is responsible for complying with Meta's Advertising
Standards and Platform Terms.
