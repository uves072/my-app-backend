from fastapi import FastAPI, APIRouter, HTTPException, Depends
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from bson import ObjectId
import socketio
import bcrypt

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Socket.IO setup
sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',
    logger=True,
    engineio_logger=True
)

# Create the main app
app = FastAPI()

# Create Socket.IO ASGI app
socket_app = socketio.ASGIApp(
    socketio_server=sio,
    other_asgi_app=app
)

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# ==================== MODELS ====================

class MenuItem(BaseModel):
    id: Optional[str] = None
    name: str
    description: str
    price: float
    category: str
    image: str  # base64 encoded image
    available: bool = True
    created_at: Optional[datetime] = None

class MenuItemCreate(BaseModel):
    name: str
    description: str
    price: float
    category: str
    image: str
    available: bool = True

class MenuItemUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    category: Optional[str] = None
    image: Optional[str] = None
    available: Optional[bool] = None

class OrderItem(BaseModel):
    menu_item_id: str
    name: str
    price: float
    quantity: int
    image: str

class Order(BaseModel):
    id: Optional[str] = None
    items: List[OrderItem]
    total: float
    status: str = "pending"  # pending, preparing, ready, completed
    table_number: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

class OrderCreate(BaseModel):
    items: List[OrderItem]
    total: float
    table_number: Optional[str] = None
    notes: Optional[str] = None

class OrderStatusUpdate(BaseModel):
    status: str

class AdminLogin(BaseModel):
    username: str
    password: str

class AdminCreate(BaseModel):
    username: str
    password: str

class AdminResponse(BaseModel):
    username: str
    token: str

# ==================== HELPER FUNCTIONS ====================

def serialize_doc(doc):
    """Convert MongoDB document to JSON serializable format"""
    if doc:
        doc['id'] = str(doc['_id'])
        del doc['_id']
    return doc

# ==================== SOCKET.IO EVENTS ====================

@sio.event
async def connect(sid, environ):
    logging.info(f"Client connected: {sid}")

@sio.event
async def disconnect(sid):
    logging.info(f"Client disconnected: {sid}")

@sio.event
async def join_admin(sid):
    """Admin joins room to receive order notifications"""
    await sio.enter_room(sid, 'admin_room')
    logging.info(f"Admin {sid} joined admin room")

# ==================== MENU ENDPOINTS ====================

@api_router.get("/menu", response_model=List[MenuItem])
async def get_menu():
    """Get all menu items"""
    try:
        items = await db.menu_items.find().to_list(1000)
        return [MenuItem(**serialize_doc(item)) for item in items]
    except Exception as e:
        logging.error(f"Error fetching menu: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/menu/categories")
async def get_categories():
    """Get all unique categories"""
    try:
        categories = await db.menu_items.distinct("category")
        return {"categories": categories}
    except Exception as e:
        logging.error(f"Error fetching categories: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/menu", response_model=MenuItem)
async def create_menu_item(item: MenuItemCreate):
    """Create a new menu item (Admin only)"""
    try:
        item_dict = item.dict()
        item_dict['created_at'] = datetime.utcnow()
        
        result = await db.menu_items.insert_one(item_dict)
        created_item = await db.menu_items.find_one({"_id": result.inserted_id})
        
        return MenuItem(**serialize_doc(created_item))
    except Exception as e:
        logging.error(f"Error creating menu item: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.put("/menu/{item_id}", response_model=MenuItem)
async def update_menu_item(item_id: str, item: MenuItemUpdate):
    """Update a menu item (Admin only)"""
    try:
        update_data = {k: v for k, v in item.dict().items() if v is not None}
        
        if not update_data:
            raise HTTPException(status_code=400, detail="No data to update")
        
        result = await db.menu_items.update_one(
            {"_id": ObjectId(item_id)},
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Menu item not found")
        
        updated_item = await db.menu_items.find_one({"_id": ObjectId(item_id)})
        return MenuItem(**serialize_doc(updated_item))
    except Exception as e:
        logging.error(f"Error updating menu item: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/menu/{item_id}")
async def delete_menu_item(item_id: str):
    """Delete a menu item (Admin only)"""
    try:
        result = await db.menu_items.delete_one({"_id": ObjectId(item_id)})
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Menu item not found")
        
        return {"message": "Menu item deleted successfully"}
    except Exception as e:
        logging.error(f"Error deleting menu item: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== ORDER ENDPOINTS ====================

@api_router.post("/orders", response_model=Order)
async def create_order(order: OrderCreate):
    """Create a new order"""
    try:
        order_dict = order.dict()
        order_dict['status'] = 'pending'
        order_dict['created_at'] = datetime.utcnow()
        
        result = await db.orders.insert_one(order_dict)
        created_order = await db.orders.find_one({"_id": result.inserted_id})
        
        order_response = Order(**serialize_doc(created_order))
        
        # Emit Socket.IO event to notify admin
        await sio.emit('new_order', order_response.dict(), room='admin_room')
        logging.info(f"New order created and emitted: {order_response.id}")
        
        return order_response
    except Exception as e:
        logging.error(f"Error creating order: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/orders", response_model=List[Order])
async def get_orders():
    """Get all orders (Admin only)"""
    try:
        orders = await db.orders.find().sort("created_at", -1).to_list(1000)
        return [Order(**serialize_doc(order)) for order in orders]
    except Exception as e:
        logging.error(f"Error fetching orders: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/orders/{order_id}", response_model=Order)
async def get_order(order_id: str):
    """Get a specific order"""
    try:
        order = await db.orders.find_one({"_id": ObjectId(order_id)})
        
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        
        return Order(**serialize_doc(order))
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error fetching order: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.put("/orders/{order_id}/status", response_model=Order)
async def update_order_status(order_id: str, status_update: OrderStatusUpdate):
    """Update order status (Admin only)"""
    try:
        valid_statuses = ["pending", "preparing", "ready", "completed"]
        if status_update.status not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")
        
        result = await db.orders.update_one(
            {"_id": ObjectId(order_id)},
            {"$set": {"status": status_update.status}}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Order not found")
        
        updated_order = await db.orders.find_one({"_id": ObjectId(order_id)})
        order_response = Order(**serialize_doc(updated_order))
        
        # Emit Socket.IO event to notify customer app
        await sio.emit('order_status_updated', order_response.dict())
        logging.info(f"Order {order_id} status updated to {status_update.status}")
        
        return order_response
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error updating order status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== ADMIN ENDPOINTS ====================

@api_router.post("/admin/register", response_model=AdminResponse)
async def register_admin(admin: AdminCreate):
    """Register a new admin"""
    try:
        # Check if username already exists
        existing_admin = await db.admins.find_one({"username": admin.username})
        if existing_admin:
            raise HTTPException(status_code=400, detail="Username already exists")
        
        # Hash password
        hashed_password = bcrypt.hashpw(admin.password.encode('utf-8'), bcrypt.gensalt())
        
        admin_dict = {
            "username": admin.username,
            "password": hashed_password.decode('utf-8'),
            "created_at": datetime.utcnow()
        }
        
        result = await db.admins.insert_one(admin_dict)
        
        # Generate token (simple version - in production use JWT)
        token = str(result.inserted_id)
        
        return AdminResponse(username=admin.username, token=token)
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error registering admin: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/admin/login", response_model=AdminResponse)
async def login_admin(admin: AdminLogin):
    """Admin login"""
    try:
        # Find admin by username
        admin_doc = await db.admins.find_one({"username": admin.username})
        
        if not admin_doc:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Verify password
        if not bcrypt.checkpw(admin.password.encode('utf-8'), admin_doc['password'].encode('utf-8')):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Generate token
        token = str(admin_doc['_id'])
        
        return AdminResponse(username=admin.username, token=token)
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error logging in admin: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== HEALTH CHECK ====================

@api_router.get("/")
async def root():
    return {"message": "Food Ordering System API", "status": "running"}

@api_router.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow()}

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
