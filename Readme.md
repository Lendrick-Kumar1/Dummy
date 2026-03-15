# Maven Careers — US Labor Statistics Dashboard

An interactive Excel dashboard exploring wage and employment trends across US states and industries from 2017 to 2020.

---

## Dashboard Preview

![Maven Careers Dashboard](dashboard_screenshot.png)

---

## Demo Video

[Watch the walkthrough on OneDrive](https://1drv.ms/v/c/9222dcd625fad717/IQBJklizv3qMRKLUiXG8KXynARz9MF_EvZH0uC6gb3HeGQI?e=k7OJrJ)

---

## 📁 Project Structure

```
maven-careers-dashboard/
├── State_Populations.xlsx       # State-level population data
├── US_Labor_Statistics.xlsx     # Labor statistics by state, industry & year
└── Maven_Careers_Dashboard.xlsx # Final interactive Excel dashboard
```

---

## 📂 Datasets

### 1. `State_Populations.xlsx`
Population figures for all US states.

| Column | Description |
|--------|-------------|
| State | US state name |
| Population | Total population count |

### 2. `US_Labor_Statistics.xlsx`
Labor market data across all 50 states and multiple industries, covering **2017–2020**.

| Column | Description |
|--------|-------------|
| Year | Year of record (2017–2020) |
| Industry | Industry sector (e.g., Natural Resources, Business Services) |
| State | US state name |
| Establishments | Number of business establishments |
| Employees | Total number of employees |
| Avg Annual Wage | Average annual wage (USD) |

**Industries covered:** Natural Resources, Business Services, Finance, Information, Manufacturing, Construction, Education & Health, Trade & Transportation, Leisure & Hospitality, Other Services

---

## Dashboard Features

- **Industry Selector** — Filter all visuals by industry using a form control
- **Average Annual Wage** — Horizontal bar chart comparing wages across all industries
- **Share of Total Employees** — Donut chart showing the selected industry's share of total employment
- **Wage & Employee Trends (2017–2020)** — Dual trend charts tracking average wage growth and total employee count over time
- **US Map** — State-level map toggling between average wage and employees per 1,000 population

---
## Key Insights

- **Information & Finance are the highest-paying industries**, averaging $94K and $90K annually — nearly 4× more than Leisure & Hospitality ($23K).
- **Leisure & Hospitality shed ~2M jobs** between 2017 and 2020 (19% decline), by far the steepest employment drop across all industries.
- **Average wages grew 14.5% over four years**, rising steadily from $53.6K in 2017 to $61.3K in 2020 across all industries and states.
