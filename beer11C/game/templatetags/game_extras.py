from django import template

register = template.Library()


@register.filter
def get_item(dictionary, key):
    """Allow dict lookups with a variable key in templates: {{ dict|get_item:key }}"""
    if not dictionary:
        return None
    return dictionary.get(key)


@register.filter
def currency(value):
    """Format a float as a currency string: {{ player.total_cost|currency }} → '$12.50'"""
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return value


_ROLE_DISPLAY = {
    'customer':    'Customer',
    'retailer':    'Retailer',
    'wholesaler':  'Wholesaler',
    'distributor': 'Distributor',
    'factory':     'Factory',
}

_ROLE_EMOJIS = {
    'customer':    '👤',
    'retailer':    '🛒',
    'wholesaler':  '🏪',
    'distributor': '🚚',
    'factory':     '🏭',
}


@register.filter
def role_display(role):
    """Return a human-readable role name: {{ ps.role|role_display }} → 'Wholesaler'"""
    return _ROLE_DISPLAY.get(role, role.title() if role else '')


@register.filter
def role_emoji(role):
    """Return the emoji for a role: {{ ps.role|role_emoji }} → '🏪'"""
    return _ROLE_EMOJIS.get(role, '❓')


_PHASE_DISPLAY = {
    'idle':    'Waiting',
    'receive': 'Receive',
    'ship':    'Ship',
    'order':   'Order',
    'done':    'Done',
}


@register.filter
def phase_display(phase):
    """Return a human-readable phase name: {{ ps.turn_phase|phase_display }} → 'Ship'"""
    return _PHASE_DISPLAY.get(phase, phase.title() if phase else '')
