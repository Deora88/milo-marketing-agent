# Milo — Privacy Policy and Terms

_Last updated: 2 November 2026_

Milo is a student internship project: an AI marketing assistant that drafts and (after human approval) publishes
Meta ad campaigns and Instagram posts, and answers Messenger and Instagram direct messages for the business that
runs it. It runs on that business's own computer, with that business's own Meta app and tokens. It is not a public
service.

## What data Milo uses

**About the business running Milo**
- **Meta ad account data:** campaigns, ad sets, ads and their results, read and written through the Meta Marketing
  API with the owner's access token.
- **Instagram posts:** captions, images, Reel videos and Story text the owner creates in Milo, and the results
  Instagram shares for them (likes, comments count, views, reach, saves, shares, replies).
- **Business documents:** PDFs the owner uploads (price lists, brochures, policies). Milo keeps only their text
  (not the file) so the DM assistant can answer from them.
- **Chat messages** typed into Milo, and **web search queries** (business type and city) used to research ad copy.

**About people who message or comment** (Facebook Page Messenger, the linked Instagram account, and comments on
their posts)
- the **messages** they send and the replies sent to them;
- their **Page-scoped id** (Messenger PSID or Instagram-scoped id), and the **name / username** Meta shares
  for that id;
- a **phone number**, if the person writes one in a message (saved as a lead so the business can call back);
- **Story replies and mentions** of the business's Instagram account (the message and a link to the Story);
- **comments** on the business's own posts (the text, the commenter's id and name/username, and the business's replies).

Milo only receives messages and comments people send to the business's own Page or Instagram account. It does not read anyone's
profile beyond the name/username above, and it uses no Facebook Login for visitors.

## Services the data is sent to
- **Meta** (Graph API) — to publish approved ads and posts, read their results, and send DM and comment replies.
- **OpenRouter** (the AI model provider) — the owner's chat messages, and incoming DMs with the business's FAQ
  and document text, so the model can write a reply.
- **Tavily** — web search queries for ad research (no personal data).
- **Cloudflare Workers AI** — image prompts (a scene description, no personal data) for AI images.

## Where data is stored
Campaigns, approvals, metrics, posts, DMs, sender names, leads, document text and the activity log are stored in
the business's MongoDB database (MongoDB Atlas). Ad and post images and Reel videos are stored in the local
`data/` folder. Access tokens and API keys stay in a local `.env` file; they are never sent to the browser or to
any service other than the one they belong to.

Post images and videos are briefly reachable at a random, unguessable web address while a post waits to be
published, because Instagram downloads them from there. They stop being served once the post is published.

## Sharing
Milo does not sell data or share it with anyone other than the services listed above, and only for the purpose
described there.

## Keeping and deleting data
- **A person who messaged the business** can ask the business to delete their messages and details; the business
  clicks **Delete** on that conversation in the Milo Inbox, which removes Milo's copy of the messages, the person's
  leads and saved name (a single lead can also be deleted from the Leads card). Comments stay on Meta; delete or
  hide them there, and Milo's copy can be removed from the `comments` collection in MongoDB Atlas.
- **Everything:** drop the `milo` database in MongoDB Atlas and delete the `data/` folder.
- **Revoke access:** remove the app in Meta Business Settings (and the Instagram connection in Instagram settings).

For questions or deletion requests, contact the developer at the email listed in the Meta app settings.

## Terms of use
Milo is provided as-is for testing and evaluation with the team's Meta business accounts. Spending is off by
default, so campaigns stay paused until the owner enables it. Every ad change and every Instagram post needs the
owner's explicit approval in the Milo dashboard. Automatic DM replies are off by default and can be switched off at
any time; a person can always take over a conversation. The owner is responsible for complying with Meta's
Advertising Standards, Platform Terms and messaging policies.
