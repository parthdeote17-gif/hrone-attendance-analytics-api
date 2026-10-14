# Employee Attendance & Analytics API

A REST API built using Python, FastAPI, and MongoDB to manage employee attendance, punch-in and punch-out, manual attendance corrections with an audit trail, and attendance analytics.

## Technologies Used

- Python 3.11+
- FastAPI
- MongoDB 6.0+
- PyMongo

## Setup

Install the required dependencies:

```bash
pip install -r requirements.txt
```

Configure your MongoDB connection using `MONGO_URI` and `MONGO_DB` in your local `.env` file. Do not commit credentials or environment files.

Load the sample data if needed:

```bash
python sample_seed.py
```

## Run the Application

```bash
uvicorn app.main:app --port 8000 --reload
```

## API Documentation

Once the application is running, open:

http://127.0.0.1:8000/docs
