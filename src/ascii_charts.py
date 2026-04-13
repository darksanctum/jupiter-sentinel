import math
from typing import List, Dict

# Unicode block characters for bar charts
BLOCKS = [' ', ' ', '▂', '▃', '▄', '▅', '▆', '▇', '█']

# ANSI Colors for terminal output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def _scale_value(val: float, min_val: float, max_val: float, height: int) -> int:
    if max_val == min_val:
        return height // 2
    return int((val - min_val) / (max_val - min_val) * (height - 1))

def render_equity_curve(data: List[float], width: int = 60, height: int = 10, title: str = "Equity Curve") -> str:
    """
    Renders a line chart for the equity curve using ASCII/Unicode characters.
    """
    if not data:
        return "No data to display"
    
    # Downsample data to fit width
    if len(data) > width:
        step = len(data) / width
        downsampled = [data[int(i * step)] for i in range(width)]
    else:
        downsampled = data
        width = len(data)
        
    min_val = min(downsampled)
    max_val = max(downsampled)
    
    grid = [[' ' for _ in range(width)] for _ in range(height)]
    
    for i, val in enumerate(downsampled):
        y = height - 1 - _scale_value(val, min_val, max_val, height)
        grid[y][i] = '•'
        # Fill below with light shading
        for j in range(y + 1, height):
            grid[j][i] = '│'

    # Build output
    out = [f"{Colors.BOLD}{Colors.CYAN}╭── {title} {'─' * max(0, width - len(title) - 1)}{Colors.RESET}"]
    for idx, row in enumerate(grid):
        y_val = max_val - (idx / (height - 1)) * (max_val - min_val) if height > 1 else max_val
        label = f"{y_val:8.2f} │"
        out.append(f"{Colors.CYAN}{label}{Colors.GREEN}{''.join(row)}{Colors.RESET}")
    out.append(f"{Colors.CYAN}╰{'─' * (width + 9)}{Colors.RESET}")
    return '\n'.join(out)


def render_candlesticks(candles: List[Dict[str, float]], width: int = 60, height: int = 15, title: str = "Candlestick Chart") -> str:
    """
    Renders a candlestick chart.
    `candles` is a list of dictionaries, each containing 'open', 'high', 'low', 'close' float values.
    """
    if not candles:
        return "No data"
        
    if len(candles) > width:
        candles = candles[-width:]
    else:
        width = len(candles)
        
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    min_val = min(lows)
    max_val = max(highs)
    
    grid = [[' ' for _ in range(width)] for _ in range(height)]
    colors_grid = [['' for _ in range(width)] for _ in range(height)]
    
    for i, c in enumerate(candles):
        o, h, l, cl = c['open'], c['high'], c['low'], c['close']
        is_bullish = cl >= o
        color = Colors.GREEN if is_bullish else Colors.RED
        
        y_high = height - 1 - _scale_value(h, min_val, max_val, height)
        y_low = height - 1 - _scale_value(l, min_val, max_val, height)
        y_open = height - 1 - _scale_value(o, min_val, max_val, height)
        y_close = height - 1 - _scale_value(cl, min_val, max_val, height)
        
        y_top_body = min(y_open, y_close)
        y_bottom_body = max(y_open, y_close)
        
        for y in range(y_high, y_low + 1):
            if y_top_body <= y <= y_bottom_body:
                char = '█'
            else:
                char = '│'
            grid[y][i] = char
            colors_grid[y][i] = color
            
    out = [f"{Colors.BOLD}{Colors.YELLOW}╭── {title} {'─' * max(0, width - len(title) - 1)}{Colors.RESET}"]
    for idx, row in enumerate(grid):
        y_val = max_val - (idx / (height - 1)) * (max_val - min_val) if height > 1 else max_val
        label = f"{y_val:8.2f} │"
        colored_row = "".join(colors_grid[idx][i] + char + Colors.RESET if char != ' ' else ' ' for i, char in enumerate(row))
        out.append(f"{Colors.YELLOW}{label}{Colors.RESET}{colored_row}")
    out.append(f"{Colors.YELLOW}╰{'─' * (width + 9)}{Colors.RESET}")
    return '\n'.join(out)

def render_volume_bars(volumes: List[float], width: int = 60, height: int = 5, title: str = "Volume") -> str:
    """
    Renders volume bars using Unicode block characters.
    """
    if not volumes:
        return "No data"
        
    if len(volumes) > width:
        step = len(volumes) / width
        downsampled = [max(volumes[int(i * step):int((i+1) * step)]) for i in range(width)]
    else:
        downsampled = volumes
        width = len(volumes)
        
    max_vol = max(downsampled) if max(downsampled) > 0 else 1
    
    out = [f"{Colors.BOLD}{Colors.BLUE}╭── {title} {'─' * max(0, width - len(title) - 1)}{Colors.RESET}"]
    
    grid = [[' ' for _ in range(width)] for _ in range(height)]
    for i, vol in enumerate(downsampled):
        normalized = (vol / max_vol) * height
        full_blocks = int(normalized)
        remainder = normalized - full_blocks
        
        for j in range(full_blocks):
            if height - 1 - j >= 0:
                grid[height - 1 - j][i] = '█'
                
        if height - 1 - full_blocks >= 0 and remainder > 0.1:
            block_idx = int(remainder * 8)
            if block_idx > 0 and block_idx < len(BLOCKS):
                grid[height - 1 - full_blocks][i] = BLOCKS[block_idx]
                
    for idx, row in enumerate(grid):
        y_val = max_vol - (idx / (height - 1)) * max_vol if height > 1 else max_vol
        label = f"{y_val:8.1f} │"
        out.append(f"{Colors.BLUE}{label}{Colors.CYAN}{''.join(row)}{Colors.RESET}")
        
    out.append(f"{Colors.BLUE}╰{'─' * (width + 9)}{Colors.RESET}")
    return '\n'.join(out)

def render_bollinger_bands(prices: List[float], upper: List[float], lower: List[float], width: int = 60, height: int = 15, title: str = "Bollinger Bands") -> str:
    """
    Renders a line chart for prices overlayed with upper and lower Bollinger Bands.
    """
    if not prices or not upper or not lower:
        return "No data"
        
    if len(prices) > width:
        prices = prices[-width:]
        upper = upper[-width:]
        lower = lower[-width:]
    else:
        width = len(prices)
        
    min_val = min(lower)
    max_val = max(upper)
    
    grid = [[' ' for _ in range(width)] for _ in range(height)]
    
    for i in range(width):
        p = prices[i]
        u = upper[i]
        l = lower[i]
        
        y_p = height - 1 - _scale_value(p, min_val, max_val, height)
        y_u = height - 1 - _scale_value(u, min_val, max_val, height)
        y_l = height - 1 - _scale_value(l, min_val, max_val, height)
        
        grid[y_p][i] = '•'
        if grid[y_u][i] == ' ': grid[y_u][i] = '⌻'
        if grid[y_l][i] == ' ': grid[y_l][i] = '⌻'
        
    out = [f"{Colors.BOLD}{Colors.WHITE}╭── {title} {'─' * max(0, width - len(title) - 1)}{Colors.RESET}"]
    for idx, row in enumerate(grid):
        y_val = max_val - (idx / (height - 1)) * (max_val - min_val) if height > 1 else max_val
        label = f"{y_val:8.2f} │"
        
        colored_row = ""
        for char in row:
            if char == '•':
                colored_row += f"{Colors.YELLOW}•{Colors.RESET}"
            elif char == '⌻':
                colored_row += f"{Colors.CYAN}⌻{Colors.RESET}"
            else:
                colored_row += " "
                
        out.append(f"{Colors.WHITE}{label}{Colors.RESET}{colored_row}")
    out.append(f"{Colors.WHITE}╰{'─' * (width + 9)}{Colors.RESET}")
    return '\n'.join(out)
