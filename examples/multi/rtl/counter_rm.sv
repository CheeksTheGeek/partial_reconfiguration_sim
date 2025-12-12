`timescale 1ns/1ps

module counter_rm (
    input wire clk,

    // Counter value - readable/writable via generated API
    output reg [31:0] counter
);

    // Counter increments every clock cycle
    // Fresh process = counter starts at 0 (PR state reset)
    always @(posedge clk) begin
        counter <= counter + 1;
    end

endmodule
