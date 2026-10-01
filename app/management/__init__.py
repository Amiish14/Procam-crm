"""
app.management — the Management Command View.

One screen for whoever is answerable for the number: what has to happen
today, what is late, what is stuck, and where the pipeline is ageing.
It defines nothing of its own — every figure is
`app/review/service.py::command_view`, which in turn reads the sales
rules, the Workbench board and the Access Matrix. The screen's only job
is to make each figure openable.
"""
