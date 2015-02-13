# -*- coding: utf-8 -*-
##############################################################################
#
#    Author: Guewen Baconnier, Sébastien Beau
#    Copyright (C) 2011 Akretion Sébastien BEAU <sebastien.beau@akretion.com>
#    Copyright 2013 Camptocamp SA (Guewen Baconnier)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp import api, models, fields, exceptions
from openerp.tools.translate import _
from collections import Iterable
import openerp.addons.decimal_precision as dp


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    @api.one
    @api.depends('amount_total', 'payment_ids.credit', 'payment_ids.debit')
    def _get_amount(self):
        paid_amount = 0
        for line in self.payment_ids:
            paid_amount += line.credit - line.debit
        self.amount_paid = paid_amount
        self.residual = self.amount_total - paid_amount

    @api.one
    @api.depends('payment_ids')
    def _payment_exists(self):
        self.payment_exists = bool(self.payment_ids)

    payment_ids = fields.Many2many(
        'account.move.line',
        string='Payments Entries')

    payment_method_id = fields.Many2one(
        'payment.method',
        'Payment Method',
        ondelete='restrict')

    residual = fields.Float(
        compute=_get_amount,
        digits_compute=dp.get_precision('Account'),
        string='Balance',
        store=False)

    amount_paid = fields.Float(
        compute=_get_amount,
        digits_compute=dp.get_precision('Account'),
        string='Amount Paid',
        store=False)

    payment_exists = fields.Boolean(
        compute=_payment_exists,
        string='Has automatic payment',
        help="It indicates that sales order has at least one payment.")

    @api.one
    def copy(self, default=None):
        if default is None:
            default = {}
        default['payment_ids'] = False
        return super(SaleOrder, self).copy(default=default)

    def automatic_payment(self, cr, uid, ids, amount=None, context=None):
        """ Create the payment entries to pay a sale order, respecting
        the payment terms.
        If no amount is defined, it will pay the residual amount of the sale
        order. """
        if isinstance(ids, Iterable):
            assert len(ids) == 1, "one sale order at a time can be paid"
            ids = ids[0]
        sale = self.browse(cr, uid, ids, context=context)
        method = sale.payment_method_id
        if not method:
            raise exceptions.Warning(
                _("An automatic payment can not be created for the sale "
                  "order %s because it has no payment method.") % sale.name
            )

        if not method.journal_id:
            raise exceptions.Warning(
                _("An automatic payment should be created for the sale order"
                  " %s but the payment method '%s' has no journal defined.") %
                (sale.name, method.name)
            )

        journal = method.journal_id
        date = sale.date_order
        if amount is None:
            amount = sale.residual
        if sale.payment_term:
            term_obj = self.pool.get('account.payment.term')
            amounts = term_obj.compute(cr, uid, sale.payment_term.id,
                                       amount, date_ref=date,
                                       context=context)
        else:
            amounts = [(date, amount)]

        # reversed is cosmetic, compute returns terms in the 'wrong' order
        for date, amount in reversed(amounts):
            self._add_payment(cr, uid, sale, journal,
                              amount, date, context=context)
        return True

    def add_payment(self, cr, uid, ids, journal_id, amount,
                    date=None, description=None, context=None):
        """ Generate payment move lines of a certain amount linked
        with the sale order. """
        if isinstance(ids, Iterable):
            assert len(ids) == 1, "one sale order at a time can be paid"
            ids = ids[0]
        journal_obj = self.pool.get('account.journal')

        sale = self.browse(cr, uid, ids, context=context)
        if date is None:
            date = sale.date_order
        journal = journal_obj.browse(cr, uid, journal_id, context=context)
        self._add_payment(cr, uid, sale, journal, amount, date, description,
                          context=context)
        return True

    def _add_payment(self, cr, uid, sale, journal, amount, date,
                     description=None, context=None):
        """ Generate move lines entries to pay the sale order. """
        move_obj = self.pool.get('account.move')
        period_obj = self.pool.get('account.period')
        period_id = period_obj.find(cr, uid, dt=date, context=context)[0]
        period = period_obj.browse(cr, uid, period_id, context=context)
        move_name = description or self._get_payment_move_name(
            cr, uid, journal,
            period, context=context)
        move_vals = self._prepare_payment_move(cr, uid, move_name, sale,
                                               journal, period, date,
                                               context=context)
        move_lines = self._prepare_payment_move_line(cr, uid, move_name, sale,
                                                     journal, period, amount,
                                                     date, context=context)

        move_vals['line_id'] = [(0, 0, line) for line in move_lines]
        move_obj.create(cr, uid, move_vals, context=context)

    def _get_payment_move_name(self, cr, uid, journal, period, context=None):
        if context is None:
            context = {}
        seq_obj = self.pool.get('ir.sequence')
        sequence = journal.sequence_id

        if not sequence:
            raise exceptions.Warning(_('Please define a sequence on the '
                                       'journal %s.') % journal.name)
        if not sequence.active:
            raise exceptions.Warning(_('Please activate the sequence of the '
                                       'journal %s.') % journal.name)

        ctx = context.copy()
        ctx['fiscalyear_id'] = period.fiscalyear_id.id
        name = seq_obj.next_by_id(cr, uid, sequence.id, context=ctx)
        return name

    def _prepare_payment_move(self, cr, uid, move_name, sale, journal,
                              period, date, context=None):
        return {'name': move_name,
                'journal_id': journal.id,
                'date': date,
                'ref': sale.name,
                'period_id': period.id,
                }

    def _prepare_payment_move_line(self, cr, uid, move_name, sale, journal,
                                   period, amount, date, context=None):
        """ """
        partner_obj = self.pool.get('res.partner')
        currency_obj = self.pool.get('res.currency')
        partner = partner_obj._find_accounting_partner(sale.partner_id)

        company = journal.company_id

        currency_id = False
        amount_currency = 0.0
        if journal.currency and journal.currency.id != company.currency_id.id:
            currency_id = journal.currency.id
            company_amount = currency_obj.compute(cr, uid,
                                                  currency_id,
                                                  company.currency_id.id,
                                                  amount,
                                                  context=context)
            amount_currency, amount = amount, company_amount

        # payment line (bank / cash)
        debit_line = {
            'name': move_name,
            'debit': amount,
            'credit': 0.0,
            'account_id': journal.default_credit_account_id.id,
            'journal_id': journal.id,
            'period_id': period.id,
            'partner_id': partner.id,
            'date': date,
            'amount_currency': amount_currency,
            'currency_id': currency_id,
        }

        # payment line (receivable)
        credit_line = {
            'name': move_name,
            'debit': 0.0,
            'credit': amount,
            'account_id': partner.property_account_receivable.id,
            'journal_id': journal.id,
            'period_id': period.id,
            'partner_id': partner.id,
            'date': date,
            'amount_currency': -amount_currency,
            'currency_id': currency_id,
            'sale_ids': [(4, sale.id)],
        }
        return debit_line, credit_line

    def onchange_payment_method_id(self, cr, uid, ids, payment_method_id,
                                   context=None):
        if not payment_method_id:
            return {}
        result = {}
        method_obj = self.pool.get('payment.method')
        method = method_obj.browse(cr, uid, payment_method_id, context=context)
        if method.payment_term_id:
            result['payment_term'] = method.payment_term_id.id
        return {'value': result}

    def action_view_payments(self, cr, uid, ids, context=None):
        """ Return an action to display the payment linked
        with the sale order """

        mod_obj = self.pool.get('ir.model.data')
        act_obj = self.pool.get('ir.actions.act_window')

        move_ids = set()
        for so in self.browse(cr, uid, ids, context=context):
            # payment_ids are move lines, we want to display the moves
            move_ids |= set([move_line.move_id.id for move_line
                             in so.payment_ids])
        move_ids = list(move_ids)

        ref = mod_obj.get_object_reference(cr, uid, 'account',
                                           'action_move_journal_line')
        action_id = False
        if ref:
            __, action_id = ref
        action = act_obj.read(cr, uid, [action_id], context=context)[0]

        # choose the view_mode accordingly
        if len(move_ids) > 1:
            action['domain'] = str([('id', 'in', move_ids)])
        else:
            ref = mod_obj.get_object_reference(cr, uid, 'account',
                                               'view_move_form')
            action['views'] = [(ref[1] if ref else False, 'form')]
            action['res_id'] = move_ids[0] if move_ids else False
        return action

    def action_cancel(self, cr, uid, ids, context=None):
        for sale in self.browse(cr, uid, ids, context=context):
            if sale.payment_ids:
                raise exceptions.Warning(_('Cannot cancel this sales order '
                                           'because automatic payment entries '
                                           'are linked with it.'))
        return super(SaleOrder, self).action_cancel(
            cr, uid, ids, context=context)
