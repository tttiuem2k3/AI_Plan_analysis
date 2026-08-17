from backend.app.models.nguon_luc import DM_CongDoanLon, DM_CongDoan, DM_NguonLuc, DM_CongDoan_Nguon_Luc, DM_CongDoan_CongDoanLon
from backend.app.schemas.nguon_luc_schema import CongDoanLonSchema, CongDoanSchema, NguonLucSchema, CongDoanNguonLucSchema, CongDoanCongDoanLonSchema
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

class CongDoanLonRepo:
    @staticmethod
    def get_all(db: Session):
        return db.query(DM_CongDoanLon).all()
    @staticmethod
    def create(db: Session, data: CongDoanLonSchema):
        obj = DM_CongDoanLon(**data.dict())
        db.add(obj)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            raise ValueError("DUPLICATE_KEY") from e
        except Exception:
            db.rollback()
            raise
        db.refresh(obj)
        return obj
    @staticmethod
    def update(db: Session, ma: str, data: CongDoanLonSchema):
        obj = db.query(DM_CongDoanLon).filter(DM_CongDoanLon.MaCongDoanLon == ma).first()
        if obj:
            for k, v in data.dict().items():
                setattr(obj, k, v)
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
            db.refresh(obj)
        return obj
    @staticmethod
    def delete(db: Session, ma: str):
        obj = db.query(DM_CongDoanLon).filter(DM_CongDoanLon.MaCongDoanLon == ma).first()
        if obj:
            try:
                # Bộ phận quản lý độc lập với công đoạn: khi xóa bộ phận chỉ gỡ mapping, không xóa công đoạn
                db.query(DM_CongDoan_CongDoanLon).filter(
                    DM_CongDoan_CongDoanLon.MaCongDoanLon == ma
                ).delete(synchronize_session=False)

                db.delete(obj)
                db.commit()
            except IntegrityError as e:
                db.rollback()
                raise ValueError("FK_CONSTRAINT") from e
            except Exception:
                db.rollback()
                raise
        return obj

class CongDoanRepo:
    @staticmethod
    def get_all(db: Session):
        return db.query(DM_CongDoan).all()

    @staticmethod
    def create(db: Session, data: CongDoanSchema):
        obj = DM_CongDoan(**data.dict())
        db.add(obj)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            raise ValueError("DUPLICATE_KEY") from e
        except Exception:
            db.rollback()
            raise
        db.refresh(obj)
        return obj

    @staticmethod
    def update(db: Session, ma: str, data: CongDoanSchema):
        obj = db.query(DM_CongDoan).filter(DM_CongDoan.MaCongDoan == ma).first()
        if obj:
            for k, v in data.dict().items():
                setattr(obj, k, v)
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
            db.refresh(obj)
        return obj

    @staticmethod
    def delete(db: Session, ma: str):
        obj = db.query(DM_CongDoan).filter(DM_CongDoan.MaCongDoan == ma).first()
        if obj:
            # Gỡ mapping công đoạn -> bộ phận trước để tránh lỗi FK
            try:
                db.query(DM_CongDoan_CongDoanLon).filter(
                    DM_CongDoan_CongDoanLon.MaCongDoan == ma
                ).delete(synchronize_session=False)

                db.delete(obj)
                db.commit()
            except IntegrityError as e:
                db.rollback()
                # Có thể còn FK ở mapping công đoạn - nguồn lực hoặc nơi khác
                raise ValueError("FK_CONSTRAINT") from e
            except Exception:
                db.rollback()
                raise
        return obj


class CongDoanCongDoanLonRepo:
    @staticmethod
    def get_all(db: Session):
        return db.query(DM_CongDoan_CongDoanLon).all()

    @staticmethod
    def attach(db: Session, data: CongDoanCongDoanLonSchema):
        # 1 công đoạn chỉ có 1 dòng mapping (PK = MaCongDoan)
        obj = DM_CongDoan_CongDoanLon(**data.dict())
        db.add(obj)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            # trùng MaCongDoan => công đoạn đã thuộc bộ phận khác
            raise ValueError("DUPLICATE_KEY") from e
        except Exception:
            db.rollback()
            raise
        db.refresh(obj)
        return obj

    @staticmethod
    def detach(db: Session, ma_cong_doan: str):
        obj = db.query(DM_CongDoan_CongDoanLon).filter(DM_CongDoan_CongDoanLon.MaCongDoan == ma_cong_doan).first()
        if obj:
            db.delete(obj)
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
        return obj

    @staticmethod
    def get_congdoanlon_of_congdoan(db: Session, ma_cong_doan: str):
        return db.query(DM_CongDoan_CongDoanLon).filter(DM_CongDoan_CongDoanLon.MaCongDoan == ma_cong_doan).first()

class NguonLucRepo:
    @staticmethod
    def get_all(db: Session):
        return db.query(DM_NguonLuc).all()

    @staticmethod
    def get_one(db: Session, ma: str):
        return db.query(DM_NguonLuc).filter(DM_NguonLuc.MaNguonLuc == ma).first()

    @staticmethod
    def create(db: Session, data: NguonLucSchema):
        obj = DM_NguonLuc(**data.dict())
        db.add(obj)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            raise ValueError("DUPLICATE_KEY") from e
        except Exception:
            db.rollback()
            raise
        db.refresh(obj)
        return obj
    @staticmethod
    def update(db: Session, ma: str, data: NguonLucSchema):
        obj = db.query(DM_NguonLuc).filter(DM_NguonLuc.MaNguonLuc == ma).first()
        if obj:
            for k, v in data.dict().items():
                setattr(obj, k, v)
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
            db.refresh(obj)
        return obj
    @staticmethod
    def delete(db: Session, ma: str):
        obj = db.query(DM_NguonLuc).filter(DM_NguonLuc.MaNguonLuc == ma).first()
        if obj:
            db.delete(obj)
            try:
                db.commit()
            except IntegrityError as e:
                db.rollback()
                # FK conflict: nguồn lực đang được dùng trong mapping
                raise ValueError("FK_CONSTRAINT") from e
            except Exception:
                db.rollback()
                raise
        return obj

class CongDoanNguonLucRepo:
    @staticmethod
    def get_all(db: Session):
        return db.query(DM_CongDoan_Nguon_Luc).all()
    @staticmethod
    def create(db: Session, data: CongDoanNguonLucSchema):
        obj = DM_CongDoan_Nguon_Luc(**data.dict())
        db.add(obj)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            raise ValueError("DUPLICATE_KEY") from e
        except Exception:
            db.rollback()
            raise
        db.refresh(obj)
        return obj
    @staticmethod
    def delete(db: Session, ma_cong_doan: str, ma_nguon_luc: str):
        obj = db.query(DM_CongDoan_Nguon_Luc).filter(
            DM_CongDoan_Nguon_Luc.MaCongDoan == ma_cong_doan,
            DM_CongDoan_Nguon_Luc.MaNguonLuc == ma_nguon_luc
        ).first()
        if obj:
            db.delete(obj)
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
        return obj

