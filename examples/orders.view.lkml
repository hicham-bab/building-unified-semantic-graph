view: orders {
  sql_table_name: ATLAS_PLATFORM.MARTS_CORE.FCT_ORDERS ;;

  dimension: order_id {
    primary_key: yes
    type: number
    sql: ${TABLE}.order_id ;;
  }
  dimension_group: order {
    type: time
    timeframes: [date, week, month, year]
    sql: ${TABLE}.order_date ;;
  }
  dimension: store_channel {
    type: string
    label: "Channel"
    sql: ${TABLE}.store_channel ;;
  }
  measure: total_gross_revenue {
    type: sum
    label: "Total Gross Revenue"
    sql: ${TABLE}.gross_revenue ;;
  }
  measure: total_orders {
    type: count_distinct
    sql: ${TABLE}.order_id ;;
  }
  measure: average_order_value {
    type: average
    sql: ${TABLE}.gross_revenue ;;
  }
}

explore: orders {
  join: stores {
    sql_on: ${orders.store_id} = ${stores.store_id} ;;
    relationship: many_to_one
    type: left_outer
  }
}
