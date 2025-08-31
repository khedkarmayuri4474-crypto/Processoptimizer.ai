# Petroleum Blend Optimizer

A web-based tool for optimizing petroleum blending with a **React frontend** and a **FastAPI backend**.  
It allows users to upload blending components, set property specifications, and calculate optimal blends using mathematical optimization.

---

## ✨ Features
- Upload component and property data (Excel/CSV)
- Set blending constraints and property limits
- Optimize blends using linear programming
- Interactive dashboard with tables and charts
- Export results as Excel reports

---

## 🛠️ Tech Stack
- **Frontend**: React + Vite + TailwindCSS
- **Backend**: FastAPI (Python) + PuLP
- **Deployment**:  
  - Backend → Render  
  - Frontend → Netlify  
  - Custom domain supported

---

## ⚙️ Installation & Setup

### Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
