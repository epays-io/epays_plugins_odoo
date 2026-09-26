# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.


def migrate(cr, version):
    """ 18.0.1.0.3: this server's IP address moves from a column of payment.provider to a system
    parameter. A column there made Apps > Upgrade fail on databases that did not have it yet.

    Databases that ran 18.0.1.0.2 have the column: its value is kept, then the column is dropped.
    """
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'payment_provider' AND column_name = 'epays_server_ip'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        SELECT epays_server_ip FROM payment_provider
         WHERE epays_server_ip IS NOT NULL AND epays_server_ip != ''
         ORDER BY write_date DESC NULLS LAST LIMIT 1
    """)
    row = cr.fetchone()
    if row:
        cr.execute("""
            INSERT INTO ir_config_parameter (key, value, create_date, write_date)
            VALUES (
                'payment_epays.server_ip', %s,
                now() at time zone 'UTC', now() at time zone 'UTC'
            )
            ON CONFLICT (key) DO NOTHING
        """, (row[0],))
    cr.execute("ALTER TABLE payment_provider DROP COLUMN epays_server_ip")
