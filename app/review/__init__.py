"""
app.review — the sales review: one person, one vertical, one meeting.

Three surfaces over the same numbers:

    /review/individual   a person's month: activity, funnel, commercial
                         result, discipline, accounts, and the trend
                         against the weeks and months before it
    /review/vertical     the same for a desk, with the drill-through a
                         review actually follows — vertical → person →
                         account → opportunity
    /review/meeting      the agenda, in the order the meeting runs, and
                         the actions it leaves behind

Nothing here defines a sales rule. "Open", "idle", "stale", "overdue",
quote ageing and the action matrix all come from
`app.services.sales_rules`; what needs attention comes from
`app.workbench.service.board`; what a deal is worth comes from
`app.services.lead_value`; who may see a record comes from
`app.access.scope`. A review that disagreed with the Workbench about
whose quote was late would be worse than no review at all.
"""
