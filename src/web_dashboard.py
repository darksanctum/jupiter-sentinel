from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn
from datetime import datetime, timedelta
import random
from jinja2 import Template

app = FastAPI(title="Jupiter Sentinel Dashboard")

def generate_mock_data():
    now = datetime.now()
    history = []
    for i in range(10):
        history.append({
            "time": (now - timedelta(minutes=i*15)).strftime("%Y-%m-%d %H:%M:%S"),
            "pair": random.choice(["SOL/USDC", "BONK/SOL", "JUP/USDC", "WIF/SOL"]),
            "type": random.choice(["BUY", "SELL"]),
            "amount": round(random.uniform(0.1, 10.0), 2),
            "price": round(random.uniform(0.001, 150.0), 4),
            "pnl": round(random.uniform(-5.0, 15.0), 2)
        })
        
    chart_labels = [(now - timedelta(hours=i)).strftime("%H:%M") for i in range(24, 0, -1)]
    chart_data = [10000]
    for _ in range(23):
        chart_data.append(chart_data[-1] * (1 + random.uniform(-0.02, 0.025)))
        
    return {
        "portfolio_value": f"${round(chart_data[-1], 2):,}",
        "24h_change": f"{round((chart_data[-1] - chart_data[0]) / chart_data[0] * 100, 2)}%",
        "open_positions": [
            {"asset": "SOL", "amount": 45.2, "value": "$6,780.00", "pnl": "+5.2%"},
            {"asset": "JUP", "amount": 1500.0, "value": "$1,200.00", "pnl": "-1.5%"},
            {"asset": "BONK", "amount": 5000000.0, "value": "$85.00", "pnl": "+12.4%"}
        ],
        "trade_history": history,
        "chart_labels": chart_labels,
        "chart_data": [round(val, 2) for val in chart_data],
        "api_status": "Operational",
        "api_latency": "124ms"
    }

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Jupiter Sentinel Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        darkbg: '#0a0a0a',
                        cardbg: '#111111',
                        bordercolor: '#1a1a1a',
                        primary: '#10b981',
                    }
                }
            }
        }
    </script>
    <style>
        body { background-color: #0a0a0a; color: #ffffff; }
        .card { background-color: #111111; border: 1px solid #1a1a1a; border-radius: 12px; padding: 1.5rem; }
    </style>
</head>
<body class="antialiased min-h-screen font-sans">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        
        <!-- Header -->
        <header class="flex justify-between items-center mb-8">
            <div class="flex items-center gap-3">
                <div class="w-10 h-10 rounded-full bg-primary flex items-center justify-center text-black font-bold text-xl">JS</div>
                <h1 class="text-2xl font-bold tracking-tight">Jupiter Sentinel</h1>
            </div>
            <div class="flex items-center gap-4">
                <div class="flex items-center gap-2 px-3 py-1 rounded-full bg-cardbg border border-bordercolor">
                    <div class="w-2 h-2 rounded-full {{ 'bg-green-500' if data.api_status == 'Operational' else 'bg-red-500' }}"></div>
                    <span class="text-sm text-gray-400">Jupiter API: {{ data.api_status }} ({{ data.api_latency }})</span>
                </div>
            </div>
        </header>

        <!-- Top Metrics -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
            <div class="card">
                <h3 class="text-gray-400 text-sm font-medium mb-1">Total Portfolio Value</h3>
                <div class="text-3xl font-bold">{{ data.portfolio_value }}</div>
                <div class="text-sm mt-2 {{ 'text-green-500' if '+' in data['24h_change'] or data['24h_change'].replace('.','').replace('%','').isdigit() else 'text-red-500' }}">
                    {{ data['24h_change'] }} (24h)
                </div>
            </div>
            <div class="card">
                <h3 class="text-gray-400 text-sm font-medium mb-1">Active Bots</h3>
                <div class="text-3xl font-bold">3</div>
                <div class="text-sm mt-2 text-green-500">Running smoothly</div>
            </div>
            <div class="card">
                <h3 class="text-gray-400 text-sm font-medium mb-1">Today's PnL</h3>
                <div class="text-3xl font-bold text-green-500">+$142.50</div>
                <div class="text-sm mt-2 text-gray-400">12 trades executed</div>
            </div>
        </div>

        <!-- Chart Section -->
        <div class="card mb-8">
            <h3 class="text-lg font-semibold mb-4">Portfolio Performance</h3>
            <div class="h-72 w-full">
                <canvas id="portfolioChart"></canvas>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
            <!-- Open Positions -->
            <div class="card">
                <h3 class="text-lg font-semibold mb-4">Open Positions</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead>
                            <tr class="text-gray-400 text-sm border-b border-bordercolor">
                                <th class="pb-3 font-medium">Asset</th>
                                <th class="pb-3 font-medium">Amount</th>
                                <th class="pb-3 font-medium">Value</th>
                                <th class="pb-3 font-medium">PnL</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-bordercolor">
                            {% for pos in data.open_positions %}
                            <tr class="text-sm">
                                <td class="py-3 font-medium">{{ pos.asset }}</td>
                                <td class="py-3">{{ pos.amount }}</td>
                                <td class="py-3">{{ pos.value }}</td>
                                <td class="py-3 {{ 'text-green-500' if '+' in pos.pnl else 'text-red-500' }}">{{ pos.pnl }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>

            <!-- Trade History -->
            <div class="card">
                <h3 class="text-lg font-semibold mb-4">Recent Trades</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead>
                            <tr class="text-gray-400 text-sm border-b border-bordercolor">
                                <th class="pb-3 font-medium">Time</th>
                                <th class="pb-3 font-medium">Pair</th>
                                <th class="pb-3 font-medium">Type</th>
                                <th class="pb-3 font-medium">Price</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-bordercolor">
                            {% for trade in data.trade_history[:5] %}
                            <tr class="text-sm">
                                <td class="py-3 text-gray-400">{{ trade.time[11:16] }}</td>
                                <td class="py-3 font-medium">{{ trade.pair }}</td>
                                <td class="py-3">
                                    <span class="px-2 py-1 rounded text-xs {{ 'bg-green-500/20 text-green-500' if trade.type == 'BUY' else 'bg-red-500/20 text-red-500' }}">
                                        {{ trade.type }}
                                    </span>
                                </td>
                                <td class="py-3">${{ trade.price }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('portfolioChart').getContext('2d');
        
        Chart.defaults.color = '#888888';
        Chart.defaults.borderColor = '#1a1a1a';
        
        const gradient = ctx.createLinearGradient(0, 0, 0, 400);
        gradient.addColorStop(0, 'rgba(16, 185, 129, 0.2)');
        gradient.addColorStop(1, 'rgba(16, 185, 129, 0)');

        new Chart(ctx, {
            type: 'line',
            data: {
                labels: {{ data.chart_labels | tojson }},
                datasets: [{
                    label: 'Portfolio Value ($)',
                    data: {{ data.chart_data | tojson }},
                    borderColor: '#10b981',
                    backgroundColor: gradient,
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHoverRadius: 4,
                    fill: true,
                    tension: 0.4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        mode: 'index',
                        intersect: false,
                        backgroundColor: '#111111',
                        titleColor: '#ffffff',
                        bodyColor: '#e5e7eb',
                        borderColor: '#1a1a1a',
                        borderWidth: 1
                    }
                },
                scales: {
                    x: {
                        grid: { display: false }
                    },
                    y: {
                        beginAtZero: false,
                        ticks: {
                            callback: function(value) {
                                return '$' + value.toLocaleString();
                            }
                        }
                    }
                },
                interaction: {
                    mode: 'nearest',
                    axis: 'x',
                    intersect: false
                }
            }
        });
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    data = generate_mock_data()
    template = Template(HTML_TEMPLATE)
    html_content = template.render(data=data)
    return HTMLResponse(content=html_content)

def start_server(host="127.0.0.1", port=8000):
    print(f"Starting web dashboard on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    start_server()
