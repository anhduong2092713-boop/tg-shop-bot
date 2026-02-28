import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import time
import random
import os
from dotenv import load_dotenv
import json
import sqlite3
# Flask保活接口依赖
from flask import Flask
import threading

# -------------------------- 1. 环境配置 --------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID", 0))
VERIFY_TIMEOUT = 60
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
CS_URL = os.getenv("CS_URL", "https://t.me/nononopoor")
PAY_INFO = {
    "alipay": os.getenv("ALIPAY", "你的支付宝账号"),
    "wechat": os.getenv("WECHAT", "你的微信账号")
}

# -------------------------- 2. Flask保活接口（解决Render端口检测） --------------------------
app = Flask(__name__)
@app.route('/')
def health_check():
    return "🤖 TG Bot is running!", 200

def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# -------------------------- 3. SQLite数据库初始化 --------------------------
def init_db():
    try:
        conn = sqlite3.connect('tg_shop_bot.db')
        cursor = conn.cursor()
        # 商品表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            pid TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            `desc` TEXT,
            specs TEXT,
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # 订单表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT,
            pid TEXT NOT NULL,
            product_name TEXT NOT NULL,
            spec TEXT,
            price REAL NOT NULL,
            status TEXT DEFAULT '待支付',
            pay_time TIMESTAMP NULL,
            ship_time TIMESTAMP NULL,
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (pid) REFERENCES products(pid)
        );
        """)
        # 卡密表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS card_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pid TEXT NOT NULL,
            spec TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            used BOOLEAN DEFAULT FALSE,
            order_id TEXT NULL,
            FOREIGN KEY (pid) REFERENCES products(pid),
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
        """)
        # 验证数据表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS verify_data (
            user_id INTEGER PRIMARY KEY,
            correct_answer INTEGER NOT NULL,
            msg_id INTEGER NOT NULL,
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # 初始化默认商品
        cursor.execute("SELECT COUNT(*) FROM products")
        if cursor.fetchone()[0] == 0:
            default_products = [
                ("p1", "🎮 游戏点卡", 100.00, 99, "通用游戏充值卡", json.dumps({"100元":100, "200元":200, "500元":500})),
                ("p2", "💎 会员月卡", 30.00, 50, "群组专属会员", json.dumps({"月卡":30, "季卡":80, "年卡":280})),
                ("p3", "📚 教程资料包", 19.90, 100, "全套教程", json.dumps({"基础版":19.9, "进阶版":49.9, "全套版":99.9})),
                ("p4", "🎁 新人礼包", 9.90, 200, "入群福利", json.dumps({"新人礼包":9.9}))
            ]
            cursor.executemany("""
            INSERT INTO products (pid, name, price, stock, `desc`, specs)
            VALUES (?, ?, ?, ?, ?, ?)
            """, default_products)
        conn.commit()
        conn.close()
        print("✅ SQLite数据库初始化完成")
    except Exception as e:
        print(f"❌ 数据库初始化失败：{e}")
        exit(1)

init_db()

# -------------------------- 4. 机器人初始化 --------------------------
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# -------------------------- 5. 工具函数 --------------------------
def db_query(sql, params=()):
    try:
        conn = sqlite3.connect('tg_shop_bot.db')
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(sql, params)
        result = [dict(row) for row in cursor.fetchall()]
        conn.commit()
        conn.close()
        return result
    except Exception as e:
        print(f"❌ 数据库查询失败：{e}")
        return []

def db_execute(sql, params=()):
    try:
        conn = sqlite3.connect('tg_shop_bot.db')
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected
    except Exception as e:
        print(f"❌ 数据库执行失败：{e}")
        return 0

def generate_order_id():
    timestamp = str(int(time.time()))[-6:]
    random_str = str(random.randint(1000, 9999))
    return f"ORD{timestamp}{random_str}"

def check_stock_warning():
    low_stock = db_query("SELECT pid, name, stock FROM products WHERE stock < 10")
    if low_stock:
        warning_text = "⚠️ 库存预警\n——————————\n"
        for p in low_stock:
            warning_text += f"{p['name']} 库存仅剩 {p['stock']} 件！\n"
        try:
            bot.send_message(ADMIN_ID, warning_text)
        except:
            pass

def auto_ship(order_id, chat_id):
    order = db_query("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    if not order:
        bot.send_message(chat_id, "❌ 订单不存在！")
        return
    order = order[0]
    card = db_query("""
    SELECT * FROM card_codes WHERE pid = ? AND spec = ? AND used = FALSE LIMIT 1
    """, (order['pid'], order['spec']))
    if not card:
        bot.send_message(chat_id, "❌ 暂无可用卡密，客服将手动发货！")
        try:
            bot.send_message(ADMIN_ID, f"⚠️ 订单 {order_id} 无可用卡密！")
        except:
            pass
        return
    card = card[0]
    db_execute("UPDATE card_codes SET used = TRUE, order_id = ? WHERE id = ?", (order_id, card['id']))
    db_execute("UPDATE orders SET status = '已支付', ship_time = CURRENT_TIMESTAMP WHERE order_id = ?", (order_id,))
    bot.send_message(chat_id, f"""
🎉 发货成功！
——————————
订单号：{order_id}
商品：{order['product_name']} - {order['spec']}
卡密：{card['code']}
——————————
💡 卡密有效期：永久
💬 客服：@nononopoor
""")

def generate_math_verify():
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    correct_answer = a + b
    options = [correct_answer]
    while len(options) < 4:
        fake = random.randint(2, 18)
        if fake != correct_answer and fake not in options:
            options.append(fake)
    random.shuffle(options)
    question = f"🔍 人机验证：{a} + {b} = ?"
    return question, correct_answer, options

# -------------------------- 6. 新人验证 --------------------------
@bot.message_handler(content_types=['new_chat_members'])
def welcome_new_member(message):
    if message.chat.id != TARGET_GROUP_ID:
        return
    for new_member in message.new_chat_members:
        if new_member.is_bot:
            continue
        user_id = new_member.id
        username = new_member.username or new_member.first_name
        question, correct_answer, options = generate_math_verify()
        verify_kb = InlineKeyboardMarkup()
        for opt in options:
            verify_kb.add(InlineKeyboardButton(str(opt), callback_data=f"verify_{opt}_{user_id}"))
        verify_msg = bot.send_message(
            chat_id=message.chat.id,
            text=f"""
🎉 欢迎 {username} 加入群组！
{VERIFY_TIMEOUT}秒内完成验证：
{question}
""",
            reply_markup=verify_kb,
            reply_to_message_id=message.message_id
        )
        db_execute("""
        INSERT INTO verify_data (user_id, correct_answer, msg_id)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET correct_answer = ?, msg_id = ?
        """, (user_id, correct_answer, verify_msg.message_id, correct_answer, verify_msg.message_id))

@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_"))
def handle_verify_answer(call):
    data_parts = call.data.split("_", 3)
    if len(data_parts) < 3:
        bot.answer_callback_query(call.id, "❌ 验证解析失败！")
        return
    _, answer_str, user_id_str = data_parts
    try:
        answer = int(answer_str)
        user_id = int(user_id_str)
    except ValueError:
        bot.answer_callback_query(call.id, "❌ 验证数据错误！")
        return
    verify_data = db_query("SELECT * FROM verify_data WHERE user_id = ?", (user_id,))
    if not verify_data:
        bot.answer_callback_query(call.id, "❌ 验证已过期！")
        return
    verify_data = verify_data[0]
    correct_answer = verify_data['correct_answer']
    verify_msg_id = verify_data['msg_id']
    username = call.from_user.username or call.from_user.first_name
    if answer == correct_answer:
        bot.answer_callback_query(call.id, "✅ 验证通过！")
        bot.send_message(
            chat_id=call.message.chat.id,
            text=f"✅ {username} 验证通过，发送 /shop 打开商城～"
        )
        db_execute("DELETE FROM verify_data WHERE user_id = ?", (user_id,))
    else:
        bot.answer_callback_query(call.id, "❌ 答案错误！")
        question, new_correct, new_options = generate_math_verify()
        new_kb = InlineKeyboardMarkup()
        for opt in new_options:
            new_kb.add(InlineKeyboardButton(str(opt), callback_data=f"verify_{opt}_{user_id}"))
        bot.edit_message_text(
            text=f"""
❌ {username} 答案错误，重新验证：
{question}
""",
            chat_id=call.message.chat.id,
            message_id=verify_msg_id,
            reply_markup=new_kb
        )
        db_execute("UPDATE verify_data SET correct_answer = ? WHERE user_id = ?", (new_correct, user_id))

# -------------------------- 7. 主菜单 --------------------------
@bot.message_handler(commands=['menu'])
def send_menu(message):
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("📜 群规", callback_data="rules"),
        InlineKeyboardButton("📞 客服", callback_data="contact")
    )
    kb.add(
        InlineKeyboardButton("❓ 帮助", callback_data="help"),
        InlineKeyboardButton("🛒 进入商城", callback_data="open_shop")
    )
    bot.send_message(message.chat.id, "✅ 功能菜单已打开：", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data in ["rules", "contact", "help"])
def callback_basic(call):
    bot.answer_callback_query(call.id)
    if call.data == "rules":
        bot.send_message(call.message.chat.id, "📜 群规：禁止广告、禁止刷屏、文明交流")
    elif call.data == "contact":
        bot.send_message(call.message.chat.id, f"📞 管理员：@nononopoor\n🔗 客服：{CS_URL}")
    elif call.data == "help":
        bot.send_message(call.message.chat.id, "❓ /menu 打开菜单 | /shop 进入商城 | /myorders 查看订单")

# -------------------------- 8. 商城功能 --------------------------
@bot.message_handler(commands=['shop'])
def open_shop_cmd(message):
    show_shop_list(message.chat.id, message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "open_shop")
def open_shop_btn(call):
    bot.answer_callback_query(call.id, "🛒 打开商城...")
    show_shop_list(call.message.chat.id, call.message.message_id)

def show_shop_list(chat_id, reply_msg_id):
    products = db_query("SELECT * FROM products")
    if not products:
        bot.send_message(chat_id, "❌ 暂无商品！")
        return
    shop_kb = InlineKeyboardMarkup()
    for p in products:
        shop_kb.add(InlineKeyboardButton(
            f"{p['name']} - ￥{p['price']}（库存：{p['stock']}）",
            callback_data=f"product_detail_{p['pid']}"
        ))
    shop_kb.add(
        InlineKeyboardButton("🔙 返回菜单", callback_data="back_menu"),
        InlineKeyboardButton("📞 客服", callback_data="contact")
    )
    bot.send_message(
        chat_id=chat_id,
        text="""
🛒 群组专属商城
——————————
选择商品（库存实时更新）：
""",
        reply_markup=shop_kb,
        reply_to_message_id=reply_msg_id
    )
    check_stock_warning()

@bot.callback_query_handler(func=lambda call: call.data.startswith("product_detail_"))
def show_product_detail(call):
    bot.answer_callback_query(call.id, "📋 加载详情...")
    pid = call.data.split("_")[-1]
    product = db_query("SELECT * FROM products WHERE pid = ?", (pid,))
    if not product:
        bot.send_message(call.message.chat.id, "❌ 商品不存在！")
        return
    product = product[0]
    specs = json.loads(product['specs'])
    spec_kb = InlineKeyboardMarkup()
    for spec_name, spec_price in specs.items():
        callback_data = f"select_spec|{pid}|{spec_name}|{spec_price}"
        spec_kb.add(InlineKeyboardButton(
            f"✅ {spec_name} - ￥{spec_price}",
            callback_data=callback_data
        ))
    spec_kb.add(InlineKeyboardButton("🔙 返回商城", callback_data="open_shop"))
    bot.send_message(
        chat_id=call.message.chat.id,
        text=f"""
📋 商品详情
——————————
名称：{product['name']}
价格：￥{product['price']}
库存：{product['stock']}件
描述：{product['desc']}
——————————
选择规格：
""",
        reply_markup=spec_kb
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_spec|"))
def confirm_order(call):
    bot.answer_callback_query(call.id, "📝 生成订单...")
    data_parts = call.data.split("|")
    if len(data_parts) != 4:
        bot.send_message(call.message.chat.id, "❌ 订单解析失败！")
        return
    _, pid, spec_name, spec_price = data_parts
    try:
        spec_price = float(spec_price)
    except ValueError:
        bot.send_message(call.message.chat.id, "❌ 价格解析失败！")
        return
    product = db_query("SELECT * FROM products WHERE pid = ?", (pid,))
    if not product:
        bot.send_message(call.message.chat.id, "❌ 商品不存在！")
        return
    product = product[0]
    if product['stock'] <= 0:
        bot.send_message(call.message.chat.id, "❌ 商品售罄！")
        return
    order_id = generate_order_id()
    user_id = call.from_user.id
    username = call.from_user.username or call.from_user.first_name
    db_execute("""
    INSERT INTO orders (order_id, user_id, username, pid, product_name, spec, price)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (order_id, user_id, username, pid, product['name'], spec_name, spec_price))
    db_execute("UPDATE products SET stock = stock - 1 WHERE pid = ?", (pid,))
    pay_kb = InlineKeyboardMarkup()
    pay_kb.add(
        InlineKeyboardButton("📞 联系客服支付", url=CS_URL),
        InlineKeyboardButton("🔍 我的订单", callback_data="my_orders")
    )
    pay_kb.add(
        InlineKeyboardButton("✅ 确认支付完成", callback_data=f"confirm_pay_{order_id}"),
        InlineKeyboardButton("🛒 返回商城", callback_data="open_shop")
    )
    bot.send_message(
        chat_id=call.message.chat.id,
        text=f"""
✅ 订单生成成功！
——————————
订单号：{order_id}
商品：{product['name']} - {spec_name}
金额：￥{spec_price}
状态：待支付
——————————
📌 支付方式：
1. 支付宝：{PAY_INFO['alipay']}（备注订单号）
2. 微信：{PAY_INFO['wechat']}（备注订单号）
3. 客服：@nononopoor 确认支付
——————————
支付后点击「确认支付完成」自动发货！
""",
        reply_markup=pay_kb
    )
    try:
        bot.send_message(ADMIN_ID, f"📝 新订单：{order_id}\n用户：{username}\n商品：{product['name']}-{spec_name}\n金额：￥{spec_price}")
    except:
        pass

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_pay_"))
def confirm_payment(call):
    data_parts = call.data.split("_", 2)
    if len(data_parts) < 2:
        bot.answer_callback_query(call.id, "❌ 订单解析失败！")
        return
    order_id = data_parts[-1]
    rowcount = db_execute("""
    UPDATE orders SET status = '已支付', pay_time = CURRENT_TIMESTAMP WHERE order_id = ?
    """, (order_id,))
    if rowcount == 0:
        bot.answer_callback_query(call.id, "❌ 订单更新失败！")
        return
    bot.answer_callback_query(call.id, "✅ 支付确认成功，发货中...")
    auto_ship(order_id, call.message.chat.id)
    try:
        bot.send_message(ADMIN_ID, f"✅ 订单 {order_id} 已支付发货！")
    except:
        pass

@bot.message_handler(commands=['myorders'])
def my_orders_cmd(message):
    show_my_orders(message.chat.id, message.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data == "my_orders")
def my_orders_btn(call):
    bot.answer_callback_query(call.id, "📜 加载订单...")
    show_my_orders(call.message.chat.id, call.from_user.id)

def show_my_orders(chat_id, user_id):
    orders = db_query("SELECT * FROM orders WHERE user_id = ? ORDER BY create_time DESC", (user_id,))
    if not orders:
        bot.send_message(chat_id, "📜 暂无订单！")
        return
    order_text = "📜 你的订单\n——————————\n"
    for o in orders:
        order_text += f"订单号：{o['order_id']}\n"
        order_text += f"商品：{o['product_name']} - {o['spec']}\n"
        order_text += f"金额：￥{o['price']}\n"
        order_text += f"状态：{o['status']}\n"
        order_text += f"时间：{o['create_time']}\n"
        if o['status'] == "待支付":
            order_text += f"💡 支付后点击「确认支付完成」\n"
        order_text += "——————————\n"
    order_kb = InlineKeyboardMarkup()
    order_kb.add(
        InlineKeyboardButton("📞 订单咨询", callback_data="contact"),
        InlineKeyboardButton("🛒 返回商城", callback_data="open_shop")
    )
    bot.send_message(chat_id, order_text, reply_markup=order_kb)

@bot.callback_query_handler(func=lambda call: call.data == "back_menu")
def back_to_menu(call):
    bot.answer_callback_query(call.id, "🔙 返回菜单...")
    send_menu(call.message)

# -------------------------- 8. 管理员功能 --------------------------
@bot.message_handler(commands=['addcard'])
def add_card_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ 无权限！")
        return
    args = message.text.split()[1:]
    if len(args) != 3:
        bot.send_message(message.chat.id, "❌ 格式：/addcard 商品ID 规格 卡密\n示例：/addcard p1 100元 ABC123456")
        return
    pid, spec, code = args
    product = db_query("SELECT * FROM products WHERE pid = ?", (pid,))
    if not product:
        bot.send_message(message.chat.id, "❌ 商品ID不存在！")
        return
    try:
        db_execute("INSERT INTO card_codes (pid, spec, code) VALUES (?, ?, ?)", (pid, spec, code))
        bot.send_message(message.chat.id, f"✅ 卡密 {code} 添加成功！")
    except sqlite3.IntegrityError:
        bot.send_message(message.chat.id, "❌ 卡密已存在！")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ 添加失败：{e}")

@bot.message_handler(commands=['allorders'])
def all_orders_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ 无权限！")
        return
    orders = db_query("SELECT * FROM orders ORDER BY create_time DESC LIMIT 20")
    if not orders:
        bot.send_message(message.chat.id, "📜 暂无订单！")
        return
    order_text = "📜 最近20条订单\n——————————\n"
    for o in orders:
        order_text += f"订单号：{o['order_id']}\n"
        order_text += f"用户：{o['username']}（ID：{o['user_id']}）\n"
        order_text += f"商品：{o['product_name']} - {o['spec']}\n"
        order_text += f"金额：￥{o['price']}\n"
        order_text += f"状态：{o['status']}\n"
        order_text += "——————————\n"
    bot.send_message(message.chat.id, order_text)

# -------------------------- 10. 优化版防休眠心跳（15分钟一次，低资源占用） --------------------------
def keep_alive_heartbeat():
    while True:
        time.sleep(850)  # 15分钟，低于Render休眠阈值
        try:
            bot.send_message(ADMIN_ID, "🔋 防休眠心跳：机器人保持活跃，避免Render休眠")
            print("✅ 防休眠心跳发送成功")
        except Exception as e:
            print(f"⚠️ 心跳发送失败：{e}")

# -------------------------- 11. 原有保活函数 --------------------------
def keep_alive():
    while True:
        time.sleep(3600)
        try:
            bot.send_message(ADMIN_ID, "🤖 机器人运行正常（心跳检测）")
        except Exception as e:
            print(f"⚠️ 心跳检测失败：{e}")

# -------------------------- 12. 主启动函数（优化版bot.polling，更抗波动） --------------------------
if __name__ == "__main__":
    if not BOT_TOKEN or ADMIN_ID == 0 or TARGET_GROUP_ID == 0:
        print("❌ 请配置环境变量：BOT_TOKEN、ADMIN_ID、TARGET_GROUP_ID")
        exit(1)
    
    # 启动Flask保活接口
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # 启动原有心跳
    keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
    keep_alive_thread.start()
    
    # 启动优化版防休眠心跳
    anti_sleep_thread = threading.Thread(target=keep_alive_heartbeat, daemon=True)
    anti_sleep_thread.start()
    
    print("🤖 机器人已启动（商城+SQLite+Render Web Service适配）")
    print("✅ HTTP 保活接口已启动，端口：", os.getenv("PORT", 8080))
    print("✅ 防休眠心跳已启动（每15分钟），避免Render免费版休眠")
    print("✅ 监听消息中...")
    
    # 优化版bot.polling循环（更长超时+更慢重连，抗波动）
    while True:
        try:
            bot.polling(
                none_stop=True,
                skip_pending=True,
                timeout=60,          # 超时从30秒延长到60秒
                long_polling_timeout=30,
                allowed_updates=["message", "callback_query", "new_chat_members"]
            )
        except Exception as e:
            print(f"⚠️ 连接断开，10秒后重连... 错误：{str(e)[:100]}")
            time.sleep(10)  # 重连间隔从5秒延长到10秒