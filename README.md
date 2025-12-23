# orchestrating simulation of multiple verilator processes for partial reconfiguration using switchboard queues

Read `examples/{multi|blinking_led}/test.py` and then run:

```console
uv pip install git+https://github.com/CheeksTheGeek/switchboard # custom switchboard fork to allow barrier syncing
```

```console
uv run examples/blinking_led/test.py
```

and

```console
uv run examples/multi/test.py
```

to understand how it works.

