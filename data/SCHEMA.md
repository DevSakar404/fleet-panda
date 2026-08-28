# Dispatch Database Schema (dispatch.db - SQLite)

## Tables

### customers
| Column | Type | Description |
|--------|------|-------------|
| customer_id | INTEGER PK | Unique customer ID |
| tenant_id | INTEGER | The tenant (fuel company) this end-customer belongs to |
| name | TEXT | End-customer name (delivery recipient) |
| region | TEXT | Geographic region |
| fleet_size | INTEGER | Nullable |
| status | TEXT | 'active' or 'inactive' |
| created_at | TEXT | ISO date |

### drivers
| Column | Type | Description |
|--------|------|-------------|
| driver_id | INTEGER PK | Unique driver ID |
| tenant_id | INTEGER | Owning tenant |
| name | TEXT | Driver full name |
| status | TEXT | 'active' or 'inactive' |
| hire_date | TEXT | ISO date |

### trucks
| Column | Type | Description |
|--------|------|-------------|
| truck_id | INTEGER PK | Unique truck ID |
| tenant_id | INTEGER | Owning tenant |
| label | TEXT | Truck label (e.g. FRG-03-002) |
| capacity_gallons | INTEGER | Tank capacity |
| status | TEXT | 'operational', 'maintenance', or 'out_of_service' |

### delivery_orders
| Column | Type | Description |
|--------|------|-------------|
| order_id | INTEGER PK | Auto-increment |
| tenant_id | INTEGER | Owning tenant |
| customer_id | INTEGER | FK to customers |
| driver_id | INTEGER | FK to drivers |
| truck_id | INTEGER | FK to trucks |
| order_date | TEXT | When the order was placed |
| delivery_date | TEXT | When delivery happened/is scheduled |
| status | TEXT | 'pending', 'in_progress', 'completed', 'cancelled' |
| product_type | TEXT | 'diesel', 'gasoline_regular', 'gasoline_premium', 'propane', 'heating_oil', 'kerosene' |
| gallons_ordered | REAL | Gallons requested |
| gallons_delivered | REAL | Gallons actually delivered (null if not completed) |
| delivery_address | TEXT | Delivery location |
| priority | TEXT | 'normal', 'urgent', 'emergency' |
| notes | TEXT | Optional delivery notes |
| created_at | TEXT | Timestamp |

### shifts
| Column | Type | Description |
|--------|------|-------------|
| shift_id | INTEGER PK | Auto-increment |
| tenant_id | INTEGER | Owning tenant |
| driver_id | INTEGER | FK to drivers |
| truck_id | INTEGER | FK to trucks |
| shift_date | TEXT | Date of shift |
| start_time | TEXT | Shift start (HH:MM) |
| end_time | TEXT | Shift end (HH:MM) |
| status | TEXT | 'completed', 'in_progress', 'cancelled' |
| total_deliveries | INTEGER | Deliveries made during shift |
| total_gallons | REAL | Total gallons delivered |
| total_miles | REAL | Total miles driven |

### tank_readings
| Column | Type | Description |
|--------|------|-------------|
| reading_id | INTEGER PK | Auto-increment |
| tenant_id | INTEGER | Owning tenant |
| customer_id | INTEGER | FK to customers |
| tank_id | TEXT | Tank identifier |
| reading_date | TEXT | Date of reading |
| level_percent | REAL | Current tank level (0-100) |
| capacity_gallons | INTEGER | Tank capacity |
| gallons_remaining | REAL | Computed remaining gallons |
| estimated_days_to_empty | REAL | Predicted days until empty |

## Key relationships
- Every table has tenant_id for multi-tenant isolation
- delivery_orders references customers, drivers, trucks
- shifts reference drivers and trucks
- tank_readings reference customers

## Data volume
- 12 tenants, ~90 days of operational data
- Thousands of delivery orders, shifts, and tank readings
