.PHONY: all clean test verilator

all: verilator

verilator:
	./test.py

test: verilator

clean:
	rm -rf build
	rm -f *.q queue-*
	rm -f *.vcd *.fst *.fst.hier
