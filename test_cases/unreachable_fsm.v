`timescale 1ns / 1ps
module unreachable_fsm (
    input wire clk,
    input wire resetn,
    input wire go,
    input wire done
);

localparam idle      = 0,
           running   = 1,
           complete  = 2,
           orphan    = 3;  // deliberately unreachable

reg [1:0] state;

always @(posedge clk) begin
    if (!resetn) begin
        state <= idle;
    end else begin
        case (state)
            idle: begin
                if (go)
                    state <= running;
            end
            running: begin
                if (done)
                    state <= complete;
                else if (go)
                    state <= idle;
            end
            complete: begin
                state <= idle;
            end
            orphan: begin
                // No state ever transitions here
                // This state is deliberately unreachable from reset
                state <= idle;
            end
            default: state <= idle;
        endcase
    end
end

endmodule
