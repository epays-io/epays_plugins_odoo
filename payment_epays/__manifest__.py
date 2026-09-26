# Part of ePays Payment Provider. See LICENSE file for full copyright and licensing details.

{
    'name': 'ePays Payment Provider',
    'version': '18.0.1.0.3',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "Cards, Benefit, BenefitPay, Apple Pay, Google Pay, Samsung Pay and Tabby through "
               "the ePays hosted payment page.",
    'author': 'ePays IT Solutions',
    'website': 'https://epays.io',
    'support': 'info@epays.io',
    'depends': ['payment'],
    'data': [
        'security/ir.model.access.csv',
        'security/ir_rules.xml',

        'views/payment_epays_templates.xml',
        'views/payment_provider_views.xml',
        'views/payment_transaction_views.xml',

        'data/payment_method_data.xml',
        'data/payment_provider_data.xml',
        'data/ir_cron.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'payment_epays/static/src/js/payment_form.js',
        ],
    },
    'images': ['static/description/banner.png'],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
