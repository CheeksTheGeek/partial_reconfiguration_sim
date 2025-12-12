.PHONY: all test clean trace

all: test

test:
	python test_blinking_led.py

trace:
	python test_blinking_led.py --trace

fast:
	python test_blinking_led.py --fast

clean:
	rm -rf build *.q *.fst *.vcd
