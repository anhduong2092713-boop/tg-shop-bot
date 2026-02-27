import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import time
import random
import os
from dotenv import load_dotenv
import json
import sqlite3  # 改用SQLite数据库（Render免费版适配）

# -------------------------- 1. 环境配置（安全隔离） --------------------------
# 加载.env文件（敏感信息不硬编码）
load_dotenv()

# 核心配置（从.env读取，优先使用环境变量）
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # 部署时必须在Render配置环境变量
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID", 0))  # 群组ID（负数）
VERIFY_TIMEOUT = 60

# 客服配置（从环境变量读取）
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))  # 管理员TG ID（纯数字）
CS_URL = os.getenv("CS_URL", "https://t.me/nononopoor")  # 客服链接
PAY_INFO = {
    "alipay": os.getenv("ALIPAY", "你的支付宝账号"),      # 可在环境变量配置
    "wechat": os.getenv("WECHAT", "你的微信账号")        # 可在环境变量配置
}

# -------------------------- 2. 初始化（SQLite数据库+无代理） --------------------------
# 初始化SQLite数据库（Python内置，无需安装MySQL）
def init_db():
    try:
        # 连接SQLite数据库（文件保存在项目根目录）
        conn = sqlite3.connect('tg_shop_bot.db')
        cursor = conn.cursor()
        # 创建商品表（适配SQLite语法）
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
        # 创建订单表
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
        # 创建卡密表（自动发货用）
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
        # 创建验证数据表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS verify_data (
            user_id INTEGER PRIMARY KEY,
            correct_answer INTEGER NOT NULL,
            msg_id INTEGER NOT NULL,
            create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # 初始化默认商品（首次运行）
        cursor.execute("SELECT COUNT(*) FROM products")
        if cursor.fetchone()[0] == 0:
            default_products = [
                ("p1", "🎮 游戏点卡", 100.00, 99, "通用游戏充值卡，支持99%手游", json.dumps({"100元":100, "200元":200, "500元":500})),
                ("p2", "💎 会员月卡", 30.00, 50, "群组专属会员，解锁全部功能", json.dumps({"月卡":30, "季卡":80, "年卡":280})),
                ("p3", "📚 教程资料包", 19.90, 100, "零基础到精通全套教程", json.dumps({"基础版":19.9, "进阶版":49.9, "全套版":99.9})),
                ("p4", "🎁 新人礼包", 9.90, 200, "入群专属新人福利，限1次", json.dumps({"新人礼包":9.9}))
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

# 执行数据库初始化
init_db()

# 初始化机器人（Render境外节点，无需SOCKS5代理）
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# -------------------------- 3. 工具函数（复用性） --------------------------
# SQLite数据库通用查询
def db_query(sql, params=()):
    try:
        conn = sqlite3.connect('tg_shop_bot.db')
        conn.row_factory = sqlite3.Row  # 返回字典格式
        cursor = conn.cursor()
        cursor.execute(sql, params)
        result = cursor.fetchall()
        # 转换为字典列表（和原MySQL逻辑兼容）
        result = [dict(row) for row in result]
        conn.commit()
        conn.close()
        return result
    except Exception as e:
        print(f"❌ 数据库查询失败：{e}")
        return []

# SQLite数据库通用执行
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

# 生成订单号
def generate_order_id():
    timestamp = str(int(time.time()))[-6:]
    random_str = str(random.randint(1000, 9999))
    return f"ORD{timestamp}{random_str}"

# 库存预警（低于10件提醒管理员）
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

# 自动发货（卡密）
def auto_ship(order_id, chat_id):
    order = db_query("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    if not order:
        bot.send_message(chat_id, "❌ 订单不存在！")
        return
    order = order[0]
    # 获取未使用的卡密
    card = db_query("""
    SELECT * FROM card_codes WHERE pid = ? AND spec = ? AND used = FALSE LIMIT 1
    """, (order['pid'], order['spec']))
    if not card:
        bot.send_message(chat_id, "❌ 暂无可用卡密，客服将手动发货！")
        try:
            bot.send_message(ADMIN_ID, f"⚠️ 订单 {order_id} 无可用卡密，需手动发货！")
        except:
            pass
        return
    card = card[0]
    # 更新卡密状态
    db_execute("UPDATE card_codes SET used = TRUE, order_id = ? WHERE id = ?", (order_id, card['id']))
    # 更新订单状态
    db_execute("UPDATE orders SET status = '已支付', ship_time = CURRENT_TIMESTAMP WHERE order_id = ?", (order_id,))
    # 发送卡密给用户
    bot.send_message(chat_id, f"""
🎉 发货成功！
——————————
订单号：{order_id}
商品：{order['product_name']} - {order['spec']}
卡密：{card['code']}
——————————
💡 卡密有效期：永久
💬 如有问题联系客服：@nononopoor
""")

# 生成验证题
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

# -------------------------- 4. 新人验证功能 --------------------------
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
为防止广告机器人，请完成以下人机验证（{VERIFY_TIMEOUT}秒内）：
{question}
""",
            reply_markup=verify_kb,
            reply_to_message_id=message.message_id
        )
        # 保存验证信息到数据库
        db_execute("""
        INSERT INTO verify_data (user_id, correct_answer, msg_id)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET correct_answer = ?, msg_id = ?
        """, (user_id, correct_answer, verify_msg.message_id, correct_answer, verify_msg.message_id))

@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_"))
def handle_verify_answer(call):
    data_parts = call.data.split("_", 3)
    if len(data_parts) < 3:
        bot.answer_callback_query(call.id, "❌ 验证解析失败，请重新进群！")
        return
    _, answer_str, user_id_str = data_parts
    try:
        answer = int(answer_str)
        user_id = int(user_id_str)
    except ValueError:
        bot.answer_callback_query(call.id, "❌ 验证数据错误，请重新进群！")
        return
    
    verify_data = db_query("SELECT * FROM verify_data WHERE user_id = ?", (user_id,))
    if not verify_data:
        bot.answer_callback_query(call.id, "❌ 验证已过期，请重新进群")
        return
    verify_data = verify_data[0]
    correct_answer = verify_data['correct_answer']
    verify_msg_id = verify_data['msg_id']
    username = call.from_user.username or call.from_user.first_name
    
    if answer == correct_answer:
        bot.answer_callback_query(call.id, "✅ 验证通过！欢迎加入群组～")
        bot.send_message(
            chat_id=call.message.chat.id,
            text=f"✅ {username} 验证通过，可正常发言！\n💡 发送 /shop 可打开群组商城～"
        )
        db_execute("DELETE FROM verify_data WHERE user_id = ?", (user_id,))
    else:
        bot.answer_callback_query(call.id, "❌ 答案错误，请重新尝试！")
        question, new_correct, new_options = generate_math_verify()
        new_kb = InlineKeyboardMarkup()
        for opt in new_options:
            new_kb.add(InlineKeyboardButton(str(opt), callback_data=f"verify_{opt}_{user_id}"))
        bot.edit_message_text(
            text=f"""
❌ {username} 答案错误，请重新验证：
{question}
""",
            chat_id=call.message.chat.id,
            message_id=verify_msg_id,
            reply_markup=new_kb
        )
        db_execute("UPDATE verify_data SET correct_answer = ? WHERE user_id = ?", (new_correct, user_id))

# -------------------------- 5. 主菜单功能 --------------------------
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
        bot.send_message(call.message.chat.id, f"📞 联系管理员：@nononopoor\n💬 商城问题也可咨询此账号\n🔗 客服直达：{CS_URL}")
    elif call.data == "help":
        bot.send_message(call.message.chat.id, "❓ 发送 /menu 打开功能菜单\n💡 发送 /shop 直接进入商城\n📝 发送 /myorders 查看我的订单")

# -------------------------- 6. 商城核心功能 --------------------------
# 6.1 打开商城
@bot.message_handler(commands=['shop'])
def open_shop_cmd(message):
    show_shop_list(message.chat.id, message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "open_shop")
def open_shop_btn(call):
    bot.answer_callback_query(call.id, "🛒 正在打开商城...")
    show_shop_list(call.message.chat.id, call.message.message_id)

# 6.2 商品列表
def show_shop_list(chat_id, reply_msg_id):
    products = db_query("SELECT * FROM products")
    if not products:
        bot.send_message(chat_id, "❌ 暂无商品数据！")
        return
    shop_kb = InlineKeyboardMarkup()
    for p in products:
        shop_kb.add(InlineKeyboardButton(
            f"{p['name']} - ￥{p['price']}（库存：{p['stock']}）",
            callback_data=f"product_detail_{p['pid']}"
        ))
    shop_kb.add(
        InlineKeyboardButton("🔙 返回菜单", callback_data="back_menu"),
        InlineKeyboardButton("📞 商城客服", callback_data="contact")
    )
    bot.send_message(
        chat_id=chat_id,
        text="""
🛒 群组专属商城
——————————
选择你想要购买的商品：
（库存实时更新，下单后自动生成订单）
""",
        reply_markup=shop_kb,
        reply_to_message_id=reply_msg_id
    )
    check_stock_warning()

# 6.3 商品详情（多规格）- 用|作为分隔符，避免拆分错误
@bot.callback_query_handler(func=lambda call: call.data.startswith("product_detail_"))
def show_product_detail(call):
    bot.answer_callback_query(call.id, "📋 加载商品详情...")
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
            f"✅ 选择 {spec_name} - ￥{spec_price}",
            callback_data=callback_data
        ))
    spec_kb.add(InlineKeyboardButton("🔙 返回商城", callback_data="open_shop"))
    bot.send_message(
        chat_id=call.message.chat.id,
        text=f"""
📋 商品详情
——————————
名称：{product['name']}
默认价格：￥{product['price']}
库存：{product['stock']}件
描述：{product['desc']}
——————————
请选择商品规格：
""",
        reply_markup=spec_kb
    )

# 6.4 确认下单 - 用|拆分，避免字段数错误
@bot.callback_query_handler(func=lambda call: call.data.startswith("select_spec|"))
def confirm_order(call):
    bot.answer_callback_query(call.id, "📝 确认订单中...")
    data_parts = call.data.split("|")
    if len(data_parts) != 4:
        bot.send_message(call.message.chat.id, "❌ 订单解析失败，请重新选择规格！")
        return
    
    _, pid, spec_name, spec_price = data_parts
    try:
        spec_price = float(spec_price)
    except ValueError:
        bot.send_message(call.message.chat.id, "❌ 价格解析失败，请重新选择规格！")
        return
    
    product = db_query("SELECT * FROM products WHERE pid = ?", (pid,))
    if not product:
        bot.send_message(call.message.chat.id, "❌ 商品不存在！")
        return
    product = product[0]
    
    if product['stock'] <= 0:
        bot.send_message(call.message.chat.id, "❌ 商品已售罄！")
        return
    
    # 生成订单
    order_id = generate_order_id()
    user_id = call.from_user.id
    username = call.from_user.username or call.from_user.first_name
    
    # 插入订单
    db_execute("""
    INSERT INTO orders (order_id, user_id, username, pid, product_name, spec, price)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (order_id, user_id, username, pid, product['name'], spec_name, spec_price))
    
    # 扣减库存
    db_execute("UPDATE products SET stock = stock - 1 WHERE pid = ?", (pid,))
    
    # 支付引导
    pay_kb = InlineKeyboardMarkup()
    pay_kb.add(
        InlineKeyboardButton("📞 联系客服支付", url=CS_URL),
        InlineKeyboardButton("🔍 查看我的订单", callback_data="my_orders")
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
创建时间：{time.strftime("%Y-%m-%d %H:%M:%S")}
——————————
📌 支付方式：
1. 支付宝：{PAY_INFO['alipay']}（备注订单号）
2. 微信：{PAY_INFO['wechat']}（备注订单号）
3. 联系客服：@nononopoor 确认支付
——————————
支付完成后点击「确认支付完成」，自动发货！
""",
        reply_markup=pay_kb
    )
    
    # 通知管理员
    try:
        bot.send_message(ADMIN_ID, f"📝 新订单提醒\n订单号：{order_id}\n用户：{username}\n商品：{product['name']} - {spec_name}\n金额：￥{spec_price}")
    except:
        pass

# 6.5 确认支付+自动发货
@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_pay_"))
def confirm_payment(call):
    data_parts = call.data.split("_", 2)
    if len(data_parts) < 2:
        bot.answer_callback_query(call.id, "❌ 订单解析失败！")
        return
    order_id = data_parts[-1]
    
    # 更新订单状态
    rowcount = db_execute("""
    UPDATE orders SET status = '已支付', pay_time = CURRENT_TIMESTAMP WHERE order_id = ?
    """, (order_id,))
    
    if rowcount == 0:
        bot.answer_callback_query(call.id, "❌ 订单状态更新失败！")
        return
    
    bot.answer_callback_query(call.id, "✅ 支付确认成功，正在发货...")
    auto_ship(order_id, call.message.chat.id)
    
    # 通知管理员
    try:
        bot.send_message(ADMIN_ID, f"✅ 订单 {order_id} 已支付，已自动发货！")
    except:
        pass

# 6.6 查看我的订单
@bot.message_handler(commands=['myorders'])
def my_orders_cmd(message):
    show_my_orders(message.chat.id, message.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data == "my_orders")
def my_orders_btn(call):
    bot.answer_callback_query(call.id, "📜 加载你的订单...")
    show_my_orders(call.message.chat.id, call.from_user.id)

def show_my_orders(chat_id, user_id):
    orders = db_query("SELECT * FROM orders WHERE user_id = ? ORDER BY create_time DESC", (user_id,))
    if not orders:
        bot.send_message(chat_id, "📜 你暂无订单记录！")
        return
    order_text = "📜 你的订单记录\n——————————\n"
    for o in orders:
        order_text += f"订单号：{o['order_id']}\n"
        order_text += f"商品：{o['product_name']} - {o['spec']}\n"
        order_text += f"金额：￥{o['price']}\n"
        order_text += f"状态：{o['status']}\n"
        order_text += f"时间：{o['create_time']}\n"
        if o['status'] == "待支付":
            order_text += f"💡 联系客服支付后点击「确认支付完成」\n"
        order_text += "——————————\n"
    order_kb = InlineKeyboardMarkup()
    order_kb.add(
        InlineKeyboardButton("📞 订单咨询", callback_data="contact"),
        InlineKeyboardButton("🛒 返回商城", callback_data="open_shop")
    )
    bot.send_message(chat_id, order_text, reply_markup=order_kb)

# 6.7 返回菜单
@bot.callback_query_handler(func=lambda call: call.data == "back_menu")
def back_to_menu(call):
    bot.answer_callback_query(call.id, "🔙 返回主菜单...")
    send_menu(call.message)

# -------------------------- 7. 管理员功能 --------------------------
# 添加卡密
@bot.message_handler(commands=['addcard'])
def add_card_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ 你无此权限！")
        return
    args = message.text.split()[1:]
    if len(args) != 3:
        bot.send_message(message.chat.id, "❌ 格式错误！正确格式：/addcard 商品ID 规格 卡密\n示例：/addcard p1 100元 ABC123456")
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

# 查看所有订单
@bot.message_handler(commands=['allorders'])
def all_orders_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ 你无此权限！")
        return
    orders = db_query("SELECT * FROM orders ORDER BY create_time DESC LIMIT 20")
    if not orders:
        bot.send_message(message.chat.id, "📜 暂无订单记录！")
        return
    order_text = "📜 最近20条订单记录\n——————————\n"
    for o in orders:
        order_text += f"订单号：{o['order_id']}\n"
        order_text += f"用户：{o['username']}（ID：{o['user_id']}）\n"
        order_text += f"商品：{o['product_name']} - {o['spec']}\n"
        order_text += f"金额：￥{o['price']}\n"
        order_text += f"状态：{o['status']}\n"
        order_text += "——————————\n"
    bot.send_message(message.chat.id, order_text)

# -------------------------- 8. 保活&启动 --------------------------
def keep_alive():
    """保活函数，每小时发送心跳"""
    while True:
        time.sleep(3600)
        try:
            bot.send_message(ADMIN_ID, "🤖 机器人运行正常（心跳检测）")
        except Exception as e:
            print(f"⚠️ 心跳检测失败：{e}")
# -------------------------- 新增：HTTP 保活接口（适配 Render Web Service） --------------------------
from flask import Flask
import threading

app = Flask(__name__)

# 简单的健康检查接口
@app.route('/')
def health_check():
    return "🤖 TG Bot is running!", 200

# 启动 Flask 服务的函数
def run_flask():
    # 使用 Render 分配的端口，没有则用 8080
    port = int(os.getenv("PORT", 8080))
    # 绑定 0.0.0.0 让 Render 能检测到端口
    app.run(host="0.0.0.0", port=port, debug=False)
# -------------------------- 必须添加：Flask 保活接口 --------------------------
from flask import Flask
import threading
import os

app = Flask(__name__)

# 健康检查接口，让 Render 能检测到端口
@app.route("/")
def hello():
    return "🤖 TG Bot is running!", 200

def run_web_server():
    # 读取 Render 自动分配的 PORT 环境变量
    port = int(os.environ.get("PORT", 8080))
    # 绑定 0.0.0.0 让外部能访问
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# -------------------------- 原有保活&启动逻辑修改 --------------------------
def keep_alive():
    while True:
        time.sleep(3600)
        try:
            bot.send_message(ADMIN_ID, "🤖 机器人运行正常（心跳检测）")
        except Exception as e:
            print(f"⚠️ 心跳检测失败：{e}")

if __name__ == "__main__":
    if not BOT_TOKEN or ADMIN_ID == 0 or TARGET_GROUP_ID == 0:
        print("❌ 请配置环境变量：BOT_TOKEN、ADMIN_ID、TARGET_GROUP_ID")
        exit(1)
    
    # 启动 Flask HTTP 服务（后台线程）
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # 启动心跳保活
    keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
    keep_alive_thread.start()
    
    print("🤖 机器人已启动（商城+SQLite+Render Web Service适配）")
    print("✅ HTTP 保活接口已启动，端口：", os.getenv("PORT", 8080))
    print("✅ 监听消息中...")
    
    # 机器人主循环
    while True:
        try:
            bot.polling(
                none_stop=True,
                skip_pending=True,
                timeout=30,
                allowed_updates=["message", "callback_query", "new_chat_members"]
            )
        except Exception as e:
            print(f"⚠️ 连接断开，5秒后重连... 错误：{str(e)[:50]}")
            time.sleep(5)


if __name__ == "__main__":
    # 校验核心环境变量
    if not BOT_TOKEN or ADMIN_ID == 0 or TARGET_GROUP_ID == 0:
        print("❌ 请配置核心环境变量：BOT_TOKEN、ADMIN_ID、TARGET_GROUP_ID")
        exit(1)
    
    print("🤖 进阶版机器人已启动（商城+SQLite+Render适配）")
    print("✅ 正在监听消息...")
    # 启动保活线程
    import threading
    threading.Thread(target=keep_alive, daemon=True).start()
    # 启动机器人（容错重连）
    while True:
        try:
            bot.polling(
                none_stop=True,
                skip_pending=True,
                timeout=30,
                allowed_updates=["message", "callback_query", "new_chat_members"]
            )
        except Exception as e:
            print(f"⚠️ 连接断开，5 秒后自动重连... 错误：{str(e)[:50]}")
            time.sleep(5)