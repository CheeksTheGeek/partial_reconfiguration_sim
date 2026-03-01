`timescale 1ns/1ps

// RM with a non-standard clock name
module weird_rm (
    input wire sys_input,
    output reg [31:0] data_out
);
    always @(posedge sys_input) begin
        data_out <= data_out + 1;
    end
endmodule
