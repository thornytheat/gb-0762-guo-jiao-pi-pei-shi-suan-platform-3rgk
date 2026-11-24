from .availability import assess, release_quantity

calculate_deduction = assess
late_receipt = assess

__all__ = ['assess', 'calculate_deduction', 'late_receipt', 'release_quantity']
