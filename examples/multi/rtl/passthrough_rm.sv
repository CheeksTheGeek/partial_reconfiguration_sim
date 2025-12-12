`timescale 1ns/1ps

module passthrough_rm (
    input wire clk,

    // Output matches partition boundary name
    output wire [31:0] counter
);

    assign counter = 32'hFFFFFFFF; // not passthrough anymore just a high value
endmodule
