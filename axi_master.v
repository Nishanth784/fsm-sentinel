//=============================================================
// axi4_slave.v (companion slave — NOT the target of analysis)
//=============================================================
module axi4_slave (
    input  wire        clk,
    input  wire         aresetn,
    input  wire        s_axi_awvalid,
    output reg          s_axi_awready
);

    localparam s_idle = 0,
               s_resp  = 1;

    reg [0:0] state;

    always @(posedge clk or negedge aresetn) begin
        if (!aresetn) begin
            state <= s_idle;
        end else begin
            case (state)
                s_idle: begin
                    if (s_axi_awvalid)
                        state <= s_resp;
                end
                s_resp: begin
                    state <= s_idle;
                end
            endcase
        end
    end

endmodule


//=============================================================
// axi_master.v — target module for FSM Sentinel analysis
//=============================================================
module axi_master (
    input  wire        clk,
    input  wire        m_axi_aresetn,
    input  wire        wr,
    input  wire        m_axi_awready,
    input  wire        m_axi_wready,
    input  wire        m_axi_bvalid,
    input  wire        m_axi_arready,
    input  wire        m_axi_rvalid,
    input  wire        m_axi_rlast,
    output reg  [3:0]  state,
    output reg  [3:0]  burst_count,
    output reg  [3:0]  waddr_count,
    output reg  [3:0]  wr_count,
    output reg  [3:0]  bresp_count,
    output reg  [3:0]  raddr_count,
    output reg  [3:0]  rd_count
);

    localparam idle             = 0,
               detect_op        = 1,
               send_waddr       = 2,
               send_wdata       = 3,
               wdata_last       = 4,
               wait_for_wr_resp = 5,
               comp_wr_tx       = 6,
               no_ack_waddr     = 7,
               no_ack_wdata     = 8,
               send_raddr       = 9,
               read_rdata       = 10,
               no_ack_raddr     = 11,
               no_ack_rdata     = 12,
               comp_rd_tx       = 13;

    always @(posedge clk or negedge m_axi_aresetn) begin
        if (!m_axi_aresetn) begin
            state <= idle;
        end else begin
            case (state)
                idle: begin
                    state <= detect_op;
                end

                detect_op: begin
                    if (wr) begin
                        state <= send_waddr;
                    end else begin
                        state <= send_raddr;
                    end
                end

                send_waddr: begin
                    if (m_axi_awready) begin
                        state <= send_wdata;
                    end else if (waddr_count == 15) begin
                        state <= no_ack_waddr;
                    end
                end

                send_wdata: begin
                    if (m_axi_wready && burst_count != 0) begin
                        state <= wdata_last;
                    end else if (wr_count == 15) begin
                        state <= no_ack_wdata;
                    end
                end

                wdata_last: begin
                    if (m_axi_wready && burst_count == 0) begin
                        state <= wait_for_wr_resp;
                    end
                end

                wait_for_wr_resp: begin
                    if (m_axi_bvalid) begin
                        state <= comp_wr_tx;
                    end else if (bresp_count == 15) begin
                        state <= idle;
                    end
                end

                comp_wr_tx: begin
                    state <= idle;
                end

                no_ack_waddr: begin
                    state <= idle;
                end

                no_ack_wdata: begin
                    state <= idle;
                end

                send_raddr: begin
                    if (m_axi_arready) begin
                        state <= read_rdata;
                    end else if (raddr_count == 15) begin
                        state <= no_ack_raddr;
                    end
                end

                read_rdata: begin
                    if (m_axi_rvalid && m_axi_rlast) begin
                        state <= comp_rd_tx;
                    end else if (rd_count == 15) begin
                        state <= no_ack_rdata;
                    end
                end

                no_ack_raddr: begin
                    state <= idle;
                end

                no_ack_rdata: begin
                    state <= idle;
                end

                comp_rd_tx: begin
                    state <= idle;
                end

                default: begin
                    state <= idle;
                end
            endcase
        end
    end

endmodule


//=============================================================
// tb_axi_master.v (testbench — NOT the target of analysis)
//=============================================================
module tb_axi_master;

    reg clk = 0;
    reg m_axi_aresetn = 0;

    localparam TB_START = 0,
               TB_RUN    = 1,
               TB_DONE   = 2;

    reg [1:0] tb_state = TB_START;

    always #5 clk = ~clk;

    initial begin
        case (tb_state)
            TB_START: tb_state = TB_RUN;
            TB_RUN:   tb_state = TB_DONE;
            TB_DONE:  ;
        endcase
    end

endmodule
