-- clear the epays master keys and move the providers off production
UPDATE payment_provider
   SET epays_master_key = NULL,
       epays_sandbox_master_key = NULL,
       epays_mode = CASE WHEN epays_mode = 'production' THEN 'sandbox' ELSE epays_mode END;

-- a copy runs on another server, with another IP address
DELETE FROM ir_config_parameter WHERE key = 'payment_epays.server_ip';
