# What changed in the CRM — one page for the sales team

## Email actually works now

The CRM has been trying to email you for months and failing silently —
a permissions problem nobody could see from the outside. It is fixed.
You will start getting notifications when a lead is assigned to you,
when a quote is overdue, and the morning action brief.

If it is too much, **you control it**: *Admin → Notification
Settings*, or go straight to `/CRM/me/notifications`. You can turn
email off entirely and keep the bell, batch it so several arrive
together, set quiet hours, or mute individual notifications. Nothing
is forced on you.

## Your lead list is no longer capped at 300

Searching used to look at only the newest 300 leads and tell you
"nothing found" for anything older. That was wrong, not just
incomplete. Search now covers every lead, and the list pages properly
— you will see "1–50 of 1,064" rather than a list that quietly stops.

## Leads are dated when they reached us

A customer's enquiry from the 1st that a colleague forwards on the
14th now shows as arriving on the 14th — because that is when it
became your work. The customer's own date is shown underneath as
**Client sent**, so you can see at a glance that the enquiry is two
weeks old.

## A closed lead reopens by itself

If you marked a lead Lost or Not Interested and the customer comes
back with a new enquiry, the CRM reopens it, moves it to RFQ
Generated and tells you. It no longer attaches the new RFQ silently
to a dead lead where nobody looks.

## Forwarded enquiries stop disappearing

When you forward a customer's email into `leads@`, every address on
it is ours — and the CRM used to decide it was internal and drop it.
Now it reads the quoted message underneath, finds the customer, and
makes the lead. If it genuinely cannot tell who the client is, it
still makes the lead and flags it rather than losing it.

**And it reads who you addressed it to.** "Dear Suranjan, please take
this up" assigns the lead to Suranjan.

## Bulk changes send one email, not forty

Reassigning forty accounts used to send forty emails. Now it sends
one, listing what changed.

## Two things to know

- **Nothing the CRM sends ever goes to a customer.** Every recipient
  is checked against the Procam domains first. If you see CRM email
  reaching a client, that is a bug worth reporting immediately.
- **The original enquiry is attached.** When a lead is assigned to
  you, the client's own email and its attachments come with it. You
  should not have to ask a colleague to forward anything.

Questions: `/CRM/help` has a page for each of these.
